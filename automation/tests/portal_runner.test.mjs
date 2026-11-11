import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { configuration, parseArgs, runDry, runPortal, validate } from "../portal_runner.mjs";

const RUNNER = fileURLToPath(new URL("../portal_runner.mjs", import.meta.url));

const CONFIG = {
  PORTAL_URL: "https://portal.example.test/login",
  PORTAL_USERNAME: "fixture-user",
  PORTAL_PASSWORD: "fixture-secret",
  PORTAL_QUERY: "AB-120 / 4",
};

// Minimal in-memory stand-in for the Playwright browser surface that
// runPortal relies on. Records every interaction so tests can assert the
// collection flow without launching a real browser or touching the network.
function fakeBrowser({ resultNode = null, evaluateError = null, closeError = null } = {}) {
  const calls = { goto: [], fills: [], clicks: [], closed: false };
  const browser = {
    async newPage() {
      return {
        async goto(url, options) {
          calls.goto.push([url, options]);
        },
        getByLabel(label) {
          return {
            async fill(value) {
              calls.fills.push([label, value]);
            },
          };
        },
        getByRole(role, options) {
          return {
            async click() {
              calls.clicks.push([role, String(options.name)]);
            },
          };
        },
        locator(selector) {
          return {
            first() {
              return {
                async evaluate(fn) {
                  if (evaluateError) throw evaluateError;
                  if (!resultNode) {
                    // Mirrors Playwright behaviour when no element matches:
                    // the locator waits, then rejects with a timeout error.
                    throw new Error(`locator.evaluate: Timeout 30000ms exceeded.\nwaiting for locator('${selector}')`);
                  }
                  return fn(resultNode);
                },
              };
            },
          };
        },
      };
    },
    async close() {
      calls.closed = true;
      if (closeError) throw closeError;
    },
  };
  return { browser, calls };
}

function captureLog() {
  const lines = [];
  return { lines, log: (line) => lines.push(line) };
}

test("successful collection emits the part as one JSON line and closes the browser", async () => {
  const node = { dataset: { partNumber: "AB1204", stockStatus: "In Stock", price: "18.50" } };
  const { browser, calls } = fakeBrowser({ resultNode: node });
  const { lines, log } = captureLog();

  await runPortal(CONFIG, { launch: async () => browser, log });

  assert.deepEqual(JSON.parse(lines[0]), { partNumber: "AB1204", stockStatus: "In Stock", price: "18.50" });
  assert.equal(lines.length, 1);
  assert.deepEqual(calls.goto, [[CONFIG.PORTAL_URL, { waitUntil: "domcontentloaded" }]]);
  assert.deepEqual(calls.fills, [
    ["Username", CONFIG.PORTAL_USERNAME],
    ["Password", CONFIG.PORTAL_PASSWORD],
    ["Part number", CONFIG.PORTAL_QUERY],
  ]);
  assert.equal(calls.clicks.length, 2);
  assert.equal(calls.closed, true);
});

test("missing data attributes fall back to unknown status and null price", async () => {
  const node = { dataset: { partNumber: "AB1204" } };
  const { browser } = fakeBrowser({ resultNode: node });
  const { lines, log } = captureLog();

  await runPortal(CONFIG, { launch: async () => browser, log });

  assert.deepEqual(JSON.parse(lines[0]), { partNumber: "AB1204", stockStatus: "unknown", price: null });
});

test("empty search result resolves to a structured empty output line", async () => {
  // Contract: downstream must be able to distinguish "portal returned no
  // offer" from "collector crashed", so an empty result should still produce
  // exactly one parseable JSON line instead of an exception.
  const { browser } = fakeBrowser({ resultNode: null });
  const { lines, log } = captureLog();

  await runPortal(CONFIG, { launch: async () => browser, log });

  assert.equal(lines.length, 1);
  assert.equal(JSON.parse(lines[0]).partNumber, null);
});

test("empty search result still closes the browser", async () => {
  const { browser, calls } = fakeBrowser({ resultNode: null });
  const { log } = captureLog();

  await assert.rejects(runPortal(CONFIG, { launch: async () => browser, log }));
  assert.equal(calls.closed, true);
});

test("browser launch failure propagates the original cause", async () => {
  const launchError = new Error("Executable doesn't exist: chromium headless shell not installed");
  await assert.rejects(runPortal(CONFIG, { launch: async () => { throw launchError; }, log: () => {} }), /headless shell not installed/);
});

test("browser close failure does not mask the original collection error", async () => {
  const { browser } = fakeBrowser({
    evaluateError: new Error("search step failed"),
    closeError: new Error("browser close failed"),
  });

  await assert.rejects(
    runPortal(CONFIG, { launch: async () => browser, log: () => {} }),
    /search step failed/,
  );
});

test("validate lists every missing configuration key", () => {
  assert.throws(() => validate(configuration({})), /missing portal configuration: PORTAL_URL, PORTAL_USERNAME, PORTAL_PASSWORD, PORTAL_QUERY/);
  assert.doesNotThrow(() => validate(configuration({
    PORTAL_URL: "https://portal.example.test",
    PORTAL_USERNAME: "u",
    PORTAL_PASSWORD: "p",
    PORTAL_QUERY: "q",
  })));
});

test("dry-run output is parseable and never leaks credentials", async () => {
  const { lines, log } = captureLog();
  await runDry(CONFIG, log);
  assert.equal(lines.length, 1);
  const payload = JSON.parse(lines[0]);
  assert.equal(payload.mode, "dry-run");
  assert.equal(payload.target, CONFIG.PORTAL_URL);
  assert.ok(!lines[0].includes(CONFIG.PORTAL_PASSWORD));
  assert.ok(!lines[0].includes(CONFIG.PORTAL_USERNAME));
});

test("parseArgs only enables dry-run via the explicit flag", () => {
  assert.deepEqual(parseArgs([]), { dryRun: false });
  assert.deepEqual(parseArgs(["--dry-run"]), { dryRun: true });
  assert.deepEqual(parseArgs(["--verbose"]), { dryRun: false });
});

test("cli --dry-run exits 0 with a JSON line and needs no configuration", () => {
  const res = spawnSync(process.execPath, [RUNNER, "--dry-run"], { env: {}, encoding: "utf8" });
  assert.equal(res.status, 0, res.stderr);
  assert.equal(JSON.parse(res.stdout.trim()).mode, "dry-run");
});

test("cli without configuration fails fast and names the missing keys", () => {
  const res = spawnSync(process.execPath, [RUNNER], { env: {}, encoding: "utf8" });
  assert.notEqual(res.status, 0);
  assert.match(res.stderr, /missing portal configuration: PORTAL_URL, PORTAL_USERNAME, PORTAL_PASSWORD, PORTAL_QUERY/);
});
