import { SessionInvalidError } from "./errors.mjs";

/**
 * In-memory fake of the portal session driver.
 *
 * Used by --dry-run (the full pool orchestration runs without a browser) and
 * by the test-suite to verify reuse, isolation and resource recycling.
 *
 * Options:
 *   loginLatencyMs     delay before a login succeeds
 *   queryLatencyMs     delay before a query resolves
 *   closeLatencyMs     delay before close() completes
 *   expireAfterQueries the (N+1)-th query on one session throws
 *                      SessionInvalidError, simulating an expired session
 *   failLogins         the first N login attempts (factory-wide) reject
 *   loginError         optional factory for the login error, receives the
 *                      credentials given to createFakeDriverFactory — useful
 *                      to prove credential redaction
 *   hangOnQueries      queries that never resolve until the driver is closed
 *                      (simulates a stuck page; close() rejects them)
 *   latencyByQuery     optional per-query latency overrides
 *
 * The returned `stats` object records logins, per-driver query/concurrency
 * counters and close counts so tests can assert pool behaviour.
 */
export function createFakeDriverFactory(options = {}) {
  const {
    loginLatencyMs = 5,
    queryLatencyMs = 10,
    closeLatencyMs = 1,
    expireAfterQueries = Infinity,
    failLogins = 0,
    loginError = null,
    hangOnQueries = [],
    latencyByQuery = {},
    credentials = null,
  } = options;

  const stats = {
    logins: 0,
    loginFailures: 0,
    drivers: [],
    activeQueries: 0,
    maxActiveQueries: 0,
  };

  const factory = async () => {
    stats.logins += 1;
    const loginSeq = stats.logins;
    await delay(loginLatencyMs);
    if (loginSeq <= failLogins) {
      stats.loginFailures += 1;
      throw loginError ? loginError(credentials) : new Error("fake login failed");
    }
    const driver = new FakeSessionDriver(loginSeq, {
      queryLatencyMs,
      closeLatencyMs,
      expireAfterQueries,
      hangOnQueries,
      latencyByQuery,
      stats,
    });
    stats.drivers.push(driver);
    return driver;
  };

  return { factory, stats };
}

class FakeSessionDriver {
  #closed = false;
  #closeHooks = new Set();

  constructor(id, { queryLatencyMs, closeLatencyMs, expireAfterQueries, hangOnQueries, latencyByQuery, stats }) {
    this.id = id;
    this.queryLatencyMs = queryLatencyMs;
    this.closeLatencyMs = closeLatencyMs;
    this.expireAfterQueries = expireAfterQueries;
    this.hangOnQueries = hangOnQueries;
    this.latencyByQuery = latencyByQuery;
    this.stats = stats;
    this.queries = [];
    this.activeQueries = 0;
    this.maxActiveQueries = 0;
    this.closeCount = 0;
  }

  get closed() {
    return this.#closed;
  }

  async runQuery(query) {
    if (this.#closed) throw new Error("fake driver is closed");
    this.queries.push(query);
    if (this.queries.length > this.expireAfterQueries) {
      throw new SessionInvalidError(`fake session ${this.id} expired`);
    }
    this.#trackConcurrency(+1);
    try {
      if (this.hangOnQueries.includes(query)) {
        // Never resolves on its own; close() rejects it, mimicking how a
        // browser teardown unblocks a stuck page operation.
        await new Promise((_, reject) => {
          this.#closeHooks.add(() => reject(new Error("fake driver closed while query was pending")));
        });
      } else {
        await delay(this.latencyByQuery[query] ?? this.queryLatencyMs);
      }
      return {
        partNumber: query.trim().toUpperCase(),
        stockStatus: "in-stock",
        price: "10.00",
        echoedQuery: query,
        sessionId: this.id,
        simulated: true,
      };
    } finally {
      this.#trackConcurrency(-1);
    }
  }

  async close() {
    this.closeCount += 1;
    if (this.#closed) return;
    this.#closed = true;
    for (const hook of this.#closeHooks) hook();
    this.#closeHooks.clear();
    await delay(this.closeLatencyMs);
  }

  #trackConcurrency(delta) {
    this.activeQueries += delta;
    this.maxActiveQueries = Math.max(this.maxActiveQueries, this.activeQueries);
    this.stats.activeQueries += delta;
    this.stats.maxActiveQueries = Math.max(this.stats.maxActiveQueries, this.stats.activeQueries);
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
