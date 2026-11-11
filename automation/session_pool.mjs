import { QueryError, isSessionInvalid } from "./errors.mjs";
import { redact } from "./credentials.mjs";

/**
 * SessionPool orchestrates a bounded number of logged-in portal sessions.
 *
 * Why: the supplier portal treats frequent logins as risky behaviour, so a
 * small set of authenticated sessions is kept alive and reused for many
 * queries instead of logging in per query.
 *
 * Guarantees:
 * - Reuse: a session is created lazily and serves queries until it fails or
 *   the pool closes; at most `size` sessions exist at any time.
 * - Isolation: one session executes one query at a time; queries never
 *   interleave on the same page.
 * - Attribution: every query carries a requestId; resolutions and every
 *   failure mode (session invalid, timeout, pool closing) are reported back
 *   with that same requestId.
 * - Recovery: SessionInvalidError recycles the session (close + fresh login)
 *   and retries the query up to `maxQueryAttempts`; a timed-out query
 *   recycles its (possibly contaminated) session and fails immediately.
 * - Close races: close() rejects queued queries with their requestIds, lets
 *   in-flight queries finish (bounded by their own timeouts), closes every
 *   session exactly once, and is idempotent.
 * - Credential hygiene: all driver errors and log lines are redacted with
 *   the configured secrets before leaving the pool.
 *
 * Driver contract (see fake_driver.mjs / portal_flow.mjs):
 *   driverFactory: () => Promise<driver>  // resolves to a logged-in driver
 *   driver.runQuery(query) => Promise<object>  // may throw SessionInvalidError
 *   driver.close() => Promise<void>  // idempotent
 */
export class SessionPool {
  #driverFactory;
  #size;
  #queryTimeoutMs;
  #loginTimeoutMs;
  #closeTimeoutMs;
  #maxQueryAttempts;
  #secrets;
  #logger;

  #state = "open"; // open -> closing -> closed
  #handles = new Set(); // every live session handle (idle or leased)
  #idle = []; // handles ready to serve a query
  #queue = []; // tasks waiting for a session
  #inFlight = 0; // queries currently executing (incl. session login)
  #drainWaiters = [];
  #closePromise = null;
  #handleSeq = 0;

  constructor({
    driverFactory,
    size = 2,
    queryTimeoutMs = 15_000,
    loginTimeoutMs = 30_000,
    closeTimeoutMs = 5_000,
    maxQueryAttempts = 2,
    secrets = [],
    logger = null,
  } = {}) {
    if (typeof driverFactory !== "function") {
      throw new TypeError("SessionPool requires a driverFactory function");
    }
    this.#driverFactory = driverFactory;
    this.#size = positiveInt(size, 2);
    this.#queryTimeoutMs = positiveInt(queryTimeoutMs, 15_000);
    this.#loginTimeoutMs = positiveInt(loginTimeoutMs, 30_000);
    this.#closeTimeoutMs = positiveInt(closeTimeoutMs, 5_000);
    this.#maxQueryAttempts = positiveInt(maxQueryAttempts, 2);
    this.#secrets = secrets.filter(Boolean);
    this.#logger = logger;
  }

  /**
   * Queue a query. Resolves with the driver result, or rejects with a
   * QueryError carrying `request.id` and a machine-readable code
   * (TIMEOUT, SESSION_INVALID, POOL_CLOSED, DRIVER_ERROR, BAD_REQUEST).
   */
  submit(request) {
    const id = request?.id;
    const query = request?.query;
    if (!id || typeof query !== "string" || query.length === 0) {
      return Promise.reject(
        new QueryError(id ?? "unknown", "BAD_REQUEST", "request requires a non-empty id and query"),
      );
    }
    if (this.#state !== "open") {
      return Promise.reject(new QueryError(id, "POOL_CLOSED", "session pool is closed"));
    }
    return new Promise((resolve, reject) => {
      this.#queue.push({ request: { id, query }, attempts: 0, resolve, reject });
      this.#pump();
    });
  }

  /**
   * Graceful shutdown. Queued queries are rejected with POOL_CLOSED (each
   * with its requestId); in-flight queries run to completion bounded by
   * their own timeouts; every session is then closed exactly once.
   * Safe to call multiple times.
   */
  close() {
    if (!this.#closePromise) this.#closePromise = this.#doClose();
    return this.#closePromise;
  }

  stats() {
    return {
      state: this.#state,
      inFlight: this.#inFlight,
      queued: this.#queue.length,
      sessions: this.#handles.size,
      idle: this.#idle.length,
    };
  }

  // -- internal ----------------------------------------------------------

  #pump() {
    if (this.#state !== "open") return;
    while (this.#queue.length > 0) {
      let handle = this.#idle.pop();
      if (!handle) {
        if (this.#handles.size >= this.#size) return;
        handle = this.#spawn();
      }
      const task = this.#queue.shift();
      this.#inFlight += 1;
      this.#execute(task, handle)
        .catch((error) => {
          // Defensive: execute() settles its task on every expected path; if a
          // bug ever lets it throw, still attribute the failure correctly.
          task.reject(this.#asQueryError(task.request.id, "DRIVER_ERROR", error, "internal pool error"));
        })
        .finally(() => {
          this.#inFlight -= 1;
          this.#pump();
          this.#checkDrained();
        });
    }
  }

  #spawn() {
    const pool = this;
    const handle = {
      id: ++this.#handleSeq,
      closed: false,
      ready: null,
      driver: null,
      async close() {
        if (handle.closed) return;
        handle.closed = true;
        pool.#handles.delete(handle);
        try {
          const driver = await handle.ready.catch(() => null);
          if (driver) {
            await withTimeout(
              () => driver.close(),
              pool.#closeTimeoutMs,
              () => new Error("driver close timed out"),
            );
          }
        } catch (error) {
          pool.#log(`session ${handle.id} close failed: ${pool.#redact(error?.message ?? error)}`);
        }
        pool.#log(`session ${handle.id} closed`);
      },
    };
    handle.ready = this.#login(handle);
    this.#handles.add(handle);
    return handle;
  }

  async #login(handle) {
    try {
      const driver = await withTimeout(
        () => this.#driverFactory(),
        this.#loginTimeoutMs,
        () => new Error("session login timed out"),
      );
      handle.driver = driver;
      this.#log(`session ${handle.id} logged in`);
      return driver;
    } catch (error) {
      this.#handles.delete(handle);
      handle.closed = true;
      throw error;
    }
  }

  async #execute(task, handle) {
    let driver;
    try {
      driver = await handle.ready;
    } catch (error) {
      await handle.close();
      task.reject(this.#asQueryError(task.request.id, "DRIVER_ERROR", error, "session login failed"));
      return;
    }

    try {
      const result = await withTimeout(
        () => driver.runQuery(task.request.query),
        this.#queryTimeoutMs,
        () => new QueryError(task.request.id, "TIMEOUT", `query timed out after ${this.#queryTimeoutMs} ms`),
      );
      task.resolve(result);
      await this.#release(handle);
    } catch (error) {
      // Any failure may have left the page in an unknown state: recycle the
      // session so the next query starts from a clean, authenticated page.
      await handle.close();
      if (error instanceof QueryError && error.code === "TIMEOUT") {
        this.#log(`request ${task.request.id} timed out; session ${handle.id} recycled`);
        task.reject(error);
      } else if (isSessionInvalid(error)) {
        task.attempts += 1;
        if (this.#state === "open" && task.attempts < this.#maxQueryAttempts) {
          this.#log(`session ${handle.id} invalidated; retrying request ${task.request.id} (attempt ${task.attempts + 1})`);
          this.#queue.unshift(task);
        } else if (this.#state === "open") {
          task.reject(this.#asQueryError(task.request.id, "SESSION_INVALID", error, "portal session expired"));
        } else {
          task.reject(new QueryError(task.request.id, "POOL_CLOSED", "pool closed before query could be retried"));
        }
      } else {
        task.reject(this.#asQueryError(task.request.id, "DRIVER_ERROR", error, "query failed"));
      }
    }
  }

  async #release(handle) {
    if (this.#state === "open" && !handle.closed) {
      this.#idle.push(handle);
    } else {
      await handle.close();
    }
  }

  async #doClose() {
    this.#state = "closing";
    const queued = this.#queue.splice(0);
    for (const task of queued) {
      task.reject(new QueryError(task.request.id, "POOL_CLOSED", "pool closed before query ran"));
    }
    if (this.#inFlight > 0) {
      await new Promise((resolve) => this.#drainWaiters.push(resolve));
    }
    await Promise.all([...this.#handles].map((handle) => handle.close()));
    this.#idle.length = 0;
    this.#state = "closed";
    this.#checkDrained();
  }

  #checkDrained() {
    if (this.#state === "open" || this.#inFlight > 0) return;
    const waiters = this.#drainWaiters.splice(0);
    for (const resolve of waiters) resolve();
  }

  #asQueryError(requestId, code, error, prefix) {
    const detail = this.#redact(error?.message ?? String(error));
    return new QueryError(requestId, code, `${prefix}: ${detail}`);
  }

  #redact(text) {
    return redact(text, this.#secrets);
  }

  #log(message) {
    if (!this.#logger) return;
    try {
      this.#logger(this.#redact(message));
    } catch {
      // A logger must never break orchestration.
    }
  }
}

/**
 * Race `fn()` against a timer. The inner promise is always subscribed by the
 * race, so a late rejection after a timeout never surfaces as an unhandled
 * rejection; the timer is cleared as soon as the operation settles, so it
 * only ever keeps the process alive while an operation is genuinely pending.
 */
function withTimeout(fn, ms, makeError) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(makeError()), ms);
  });
  return Promise.race([Promise.resolve().then(fn), timeout]).finally(() => clearTimeout(timer));
}

function positiveInt(value, fallback) {
  return Number.isFinite(value) && value >= 1 ? Math.floor(value) : fallback;
}
