import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const runnerPath = fileURLToPath(new URL("../portal_runner.mjs", import.meta.url));

async function runCli(args, env = {}) {
  try {
    const { stdout, stderr } = await execFileAsync(process.execPath, [runnerPath, ...args], { env });
    return { code: 0, stdout, stderr };
  } catch (error) {
    return { code: error.code, stdout: error.stdout ?? "", stderr: error.stderr ?? "" };
  }
}

const parseJsonl = (stdout) => stdout.trim().split("\n").filter(Boolean).map(JSON.parse);

test("dry-run: every query gets an attributed envelope on stdout", async () => {
  const { code, stdout, stderr } = await runCli([
    "--dry-run",
    "--query", "ab-120",
    "--query", "zz-9",
    "--request-id", "r-1",
    "--request-id", "r-2",
  ]);

  assert.equal(code, 0, stderr);
  const envelopes = parseJsonl(stdout);
  assert.deepEqual(
    envelopes.map((envelope) => envelope.requestId),
    ["r-1", "r-2"],
  );
  assert.deepEqual(
    envelopes.map((envelope) => envelope.status),
    ["ok", "ok"],
  );
  assert.equal(envelopes[0].data.echoedQuery, "ab-120");
  assert.equal(envelopes[1].data.echoedQuery, "zz-9");
  assert.equal(envelopes[0].data.simulated, true);
});

test("dry-run: works with no configuration and still attributes the simulated query", async () => {
  const { code, stdout } = await runCli(["--dry-run"]);

  assert.equal(code, 0);
  const envelopes = parseJsonl(stdout);
  assert.equal(envelopes.length, 1);
  assert.equal(envelopes[0].requestId, "req-1");
  assert.equal(envelopes[0].status, "ok");
});

test("dry-run: queries from a JSON file keep their ids", async () => {
  const dir = await mkdtemp(join(tmpdir(), "portal-runner-"));
  const file = join(dir, "queries.json");
  await writeFile(file, JSON.stringify([{ id: "q-1", query: "AA-1" }, "BB-2"]));

  const { code, stdout } = await runCli(["--dry-run", "--queries-file", file]);

  assert.equal(code, 0);
  const envelopes = parseJsonl(stdout);
  assert.deepEqual(
    envelopes.map((envelope) => envelope.requestId),
    ["q-1", "req-2"],
  );
  assert.equal(envelopes[0].data.echoedQuery, "AA-1");
  assert.equal(envelopes[1].data.echoedQuery, "BB-2");
});

test("real mode: missing configuration reports names only, never secret values", async () => {
  const { code, stdout, stderr } = await runCli(["--query", "ab-120"], {
    PORTAL_USERNAME: "sentinel-user",
    PORTAL_PASSWORD: "sentinel-pass",
  });

  assert.equal(code, 2);
  assert.equal(stdout, "");
  assert.match(stderr, /PORTAL_URL/);
  assert.ok(!stderr.includes("sentinel-user"), "stderr leaked the username");
  assert.ok(!stderr.includes("sentinel-pass"), "stderr leaked the password");
});

test("usage errors exit with code 2 and no secret leakage", async () => {
  const { code, stderr } = await runCli(["--request-id", "r-1"], {
    PORTAL_USERNAME: "sentinel-user",
    PORTAL_PASSWORD: "sentinel-pass",
  });

  assert.equal(code, 2);
  assert.ok(!stderr.includes("sentinel-user"));
  assert.ok(!stderr.includes("sentinel-pass"));
});
