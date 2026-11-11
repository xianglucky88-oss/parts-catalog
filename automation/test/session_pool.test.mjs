import test from "node:test";
import assert from "node:assert/strict";
import { SessionPool } from "../session_pool.mjs";
import { createFakeDriverFactory } from "../fake_driver.mjs";
import { Credentials, redact } from "../credentials.mjs";

const settle = (promise) =>
  promise.then(
    (data) => ({ status: "ok", data }),
    (error) => ({ status: "error", code: error.code, requestId: error.requestId, message: error.message }),
  );

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const makeRequests = (count) =>
  Array.from({ length: count }, (_, index) => ({ id: `req-${index + 1}`, query: `part-${index + 1}` }));

test("reuse: one logged-in session serves many queries", async () => {
  const { factory, stats } = createFakeDriverFactory({ queryLatencyMs: 2 });
  const pool = new SessionPool({ driverFactory: factory, size: 1, queryTimeoutMs: 1000 });
  const requests = makeRequests(5);

  const results = await Promise.all(requests.map((request) => settle(pool.submit(request))));
  await pool.close();

  assert.equal(stats.logins, 1);
  assert.equal(stats.drivers.length, 1);
  for (const [index, result] of results.entries()) {
    assert.equal(result.status, "ok");
    assert.equal(result.data.echoedQuery, requests[index].query);
    assert.equal(result.data.sessionId, 1);
  }
});

test("isolation: concurrency is bounded and a session never runs two queries at once", async () => {
  const { factory, stats } = createFakeDriverFactory({ queryLatencyMs: 20 });
  const pool = new SessionPool({ driverFactory: factory, size: 2, queryTimeoutMs: 2000 });
  const requests = makeRequests(6);

  const results = await Promise.all(requests.map((request) => settle(pool.submit(request))));
  await pool.close();

  assert.equal(stats.logins, 2);
  assert.ok(stats.maxActiveQueries <= 2, `pool-wide concurrency ${stats.maxActiveQueries} exceeds size`);
  for (const driver of stats.drivers) {
    assert.equal(driver.maxActiveQueries, 1, `session ${driver.id} interleaved queries`);
  }
  for (const [index, result] of results.entries()) {
    assert.equal(result.status, "ok");
    assert.equal(result.data.echoedQuery, requests[index].query);
  }
});

test("attribution: results map to their requestId even when queries finish out of order", async () => {
  const { factory } = createFakeDriverFactory({ latencyByQuery: { SLOWISH: 60, QUICK: 5 } });
  const pool = new SessionPool({ driverFactory: factory, size: 2, queryTimeoutMs: 2000 });

  const slow = settle(pool.submit({ id: "req-slow", query: "SLOWISH" }));
  const quick = settle(pool.submit({ id: "req-quick", query: "QUICK" }));
  const [slowResult, quickResult] = await Promise.all([slow, quick]);
  await pool.close();

  assert.equal(slowResult.status, "ok");
  assert.equal(slowResult.data.echoedQuery, "SLOWISH");
  assert.equal(quickResult.status, "ok");
  assert.equal(quickResult.data.echoedQuery, "QUICK");
});

test("recycling: an invalidated session is re-logged-in and the query retried with its requestId", async () => {
  const { factory, stats } = createFakeDriverFactory({ expireAfterQueries: 2, queryLatencyMs: 2 });
  const pool = new SessionPool({ driverFactory: factory, size: 1, queryTimeoutMs: 1000 });
  const requests = makeRequests(4);

  const results = await Promise.all(requests.map((request) => settle(pool.submit(request))));
  await pool.close();

  assert.equal(stats.logins, 2);
  assert.deepEqual(
    results.map((result) => result.status),
    ["ok", "ok", "ok", "ok"],
  );
  assert.deepEqual(
    results.map((result) => result.data.sessionId),
    [1, 1, 2, 2],
  );
  assert.deepEqual(
    results.map((result) => result.data.echoedQuery),
    requests.map((request) => request.query),
  );
  assert.equal(stats.drivers[0].closeCount, 1, "invalidated session must be closed exactly once");
});

test("recycling: repeated session invalidation fails as SESSION_INVALID after bounded retries", async () => {
  const { factory, stats } = createFakeDriverFactory({ expireAfterQueries: 0 });
  const pool = new SessionPool({ driverFactory: factory, size: 1, maxQueryAttempts: 2, queryTimeoutMs: 1000 });

  const result = await settle(pool.submit({ id: "req-x", query: "part-x" }));
  await pool.close();

  assert.equal(result.status, "error");
  assert.equal(result.code, "SESSION_INVALID");
  assert.equal(result.requestId, "req-x");
  assert.equal(stats.logins, 2, "one retry means one extra login");
});

test("timeout: a stuck query fails with TIMEOUT, its session is recycled, later queries unaffected", async () => {
  const { factory, stats } = createFakeDriverFactory({ hangOnQueries: ["SLOW"], queryLatencyMs: 2 });
  const pool = new SessionPool({ driverFactory: factory, size: 1, queryTimeoutMs: 60 });

  const slow = settle(pool.submit({ id: "req-slow", query: "SLOW" }));
  const fast = settle(pool.submit({ id: "req-fast", query: "FAST" }));
  const [slowResult, fastResult] = await Promise.all([slow, fast]);
  await pool.close();

  assert.equal(slowResult.status, "error");
  assert.equal(slowResult.code, "TIMEOUT");
  assert.equal(slowResult.requestId, "req-slow");
  assert.ok(!slowResult.message.includes("req-fast"));
  assert.equal(fastResult.status, "ok");
  assert.equal(fastResult.data.echoedQuery, "FAST");
  assert.equal(stats.logins, 2, "contaminated session must be replaced");
  assert.equal(stats.drivers[0].closeCount, 1);
});

test("close race: queued queries fail with their requestIds, in-flight query completes, close is idempotent", async () => {
  const { factory, stats } = createFakeDriverFactory({ queryLatencyMs: 40 });
  const pool = new SessionPool({ driverFactory: factory, size: 1, queryTimeoutMs: 2000 });

  const a = settle(pool.submit({ id: "req-a", query: "A" }));
  const b = settle(pool.submit({ id: "req-b", query: "B" }));
  const c = settle(pool.submit({ id: "req-c", query: "C" }));
  await delay(10);
  const closing = pool.close();
  const secondClose = pool.close();
  assert.equal(closing, secondClose, "close() must return the same promise");

  const [ra, rb, rc] = await Promise.all([a, b, c]);
  await closing;

  assert.equal(ra.status, "ok");
  assert.equal(ra.data.echoedQuery, "A");
  assert.equal(rb.status, "error");
  assert.equal(rb.code, "POOL_CLOSED");
  assert.equal(rb.requestId, "req-b");
  assert.equal(rc.code, "POOL_CLOSED");
  assert.equal(rc.requestId, "req-c");
  for (const driver of stats.drivers) {
    assert.equal(driver.closeCount, 1, `session ${driver.id} closed ${driver.closeCount} times`);
  }
  assert.equal(pool.stats().state, "closed");

  const late = await settle(pool.submit({ id: "req-late", query: "L" }));
  assert.equal(late.code, "POOL_CLOSED");
  assert.equal(late.requestId, "req-late");
});

test("close race: close() during a stuck query is bounded by the query timeout", { timeout: 5000 }, async () => {
  const { factory, stats } = createFakeDriverFactory({ hangOnQueries: ["HANG"] });
  const pool = new SessionPool({ driverFactory: factory, size: 1, queryTimeoutMs: 80 });

  const hung = settle(pool.submit({ id: "req-hang", query: "HANG" }));
  await delay(20);
  const startedAt = Date.now();
  await pool.close();
  const elapsed = Date.now() - startedAt;

  const result = await hung;
  assert.equal(result.code, "TIMEOUT");
  assert.equal(result.requestId, "req-hang");
  assert.ok(elapsed < 1000, `close() took ${elapsed}ms despite the stuck query`);
  assert.equal(stats.drivers[0].closeCount, 1);
});

test("credentials: driver errors and pool logs never contain secrets", async () => {
  const credentials = new Credentials("s3cr3t-user", "p@ss-w0rd!");
  const { factory } = createFakeDriverFactory({
    credentials,
    failLogins: Number.MAX_SAFE_INTEGER,
    loginError: (leaked) => new Error(`login failed for user=${leaked.username} pass=${leaked.password}`),
  });
  const logLines = [];
  const pool = new SessionPool({
    driverFactory: factory,
    size: 1,
    queryTimeoutMs: 1000,
    secrets: credentials.secrets,
    logger: (line) => logLines.push(line),
  });

  const result = await settle(pool.submit({ id: "req-sec", query: "part-sec" }));
  await pool.close();

  assert.equal(result.status, "error");
  assert.equal(result.code, "DRIVER_ERROR");
  assert.equal(result.requestId, "req-sec");
  assert.ok(!result.message.includes("s3cr3t-user"), `error leaked username: ${result.message}`);
  assert.ok(!result.message.includes("p@ss-w0rd!"), `error leaked password: ${result.message}`);
  for (const line of logLines) {
    assert.ok(!line.includes("s3cr3t-user"), `log leaked username: ${line}`);
    assert.ok(!line.includes("p@ss-w0rd!"), `log leaked password: ${line}`);
  }
});

test("credentials: the container and redact() cannot be coerced into revealing secrets", () => {
  const credentials = new Credentials("user-x", "pass-y");
  assert.equal(JSON.stringify({ credentials }), '{"credentials":"[redacted]"}');
  assert.equal(`${credentials}`, "[redacted]");
  assert.equal(redact("token pass-y leaked by user-x", credentials.secrets), "token *** leaked by ***");
  assert.equal(redact("nothing to hide", credentials.secrets), "nothing to hide");
});

test("validation: malformed requests are rejected without spawning a session", async () => {
  const { factory, stats } = createFakeDriverFactory();
  const pool = new SessionPool({ driverFactory: factory });

  const missingQuery = await settle(pool.submit({ id: "req-bad" }));
  const missingId = await settle(pool.submit({ query: "part" }));
  await pool.close();

  assert.equal(missingQuery.code, "BAD_REQUEST");
  assert.equal(missingQuery.requestId, "req-bad");
  assert.equal(missingId.code, "BAD_REQUEST");
  assert.equal(stats.logins, 0);
});
