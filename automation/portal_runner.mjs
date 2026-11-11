import process from "node:process";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { Credentials, redact } from "./credentials.mjs";
import { SessionPool } from "./session_pool.mjs";
import { createFakeDriverFactory } from "./fake_driver.mjs";

const USAGE = `Usage: node portal_runner.mjs [options]

Options:
  --dry-run               Run the full session-pool orchestration against a
                          local fake driver; no browser, no network, no env
                          required.
  --query <part-number>   Query to execute (repeatable).
  --request-id <id>       Correlation id for the corresponding --query
                          (repeatable, positional; defaults to req-<n>).
  --queries-file <path>   JSON array of queries: strings or {id, query}.
  --pool-size <n>         Max simultaneous logged-in sessions (default:
                          PORTAL_POOL_SIZE or 2).
  --query-timeout-ms <ms> Per-query timeout (default: PORTAL_QUERY_TIMEOUT_MS
                          or 15000).
  -h, --help              Show this help.

Environment (real mode only): PORTAL_URL, PORTAL_USERNAME, PORTAL_PASSWORD;
PORTAL_QUERY is still honoured when no --query is given. Credentials are
never logged and never included in error output.

Output: one JSON object per line on stdout, each carrying the requestId of
its request: {"requestId":"...","status":"ok","data":{...}} or
{"requestId":"...","status":"error","error":{"code":"...","message":"..."}}.
Exit code: 0 if every query succeeded, 1 if any query failed, 2 for
usage/configuration errors.`;

const REQUIRED_ENV = ["PORTAL_URL", "PORTAL_USERNAME", "PORTAL_PASSWORD"];

function parseArgs(argv) {
  const args = {
    dryRun: false,
    help: false,
    queries: [],
    requestIds: [],
    queriesFile: null,
    poolSize: null,
    queryTimeoutMs: null,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = () => {
      const next = argv[++i];
      if (next === undefined) throw new Error(`missing value for ${arg}`);
      return next;
    };
    switch (arg) {
      case "--dry-run":
        args.dryRun = true;
        break;
      case "--query":
        args.queries.push(value());
        break;
      case "--request-id":
        args.requestIds.push(value());
        break;
      case "--queries-file":
        args.queriesFile = value();
        break;
      case "--pool-size":
        args.poolSize = Number(value());
        break;
      case "--query-timeout-ms":
        args.queryTimeoutMs = Number(value());
        break;
      case "-h":
      case "--help":
        args.help = true;
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  if (args.requestIds.length > 0 && args.requestIds.length !== args.queries.length) {
    throw new Error("--request-id must be given exactly once per --query");
  }
  return args;
}

async function loadRequests(args, env) {
  const requests = [];
  if (args.queriesFile) {
    const entries = JSON.parse(await readFile(args.queriesFile, "utf8"));
    if (!Array.isArray(entries)) throw new Error("queries file must contain a JSON array");
    for (const [index, entry] of entries.entries()) {
      if (typeof entry === "string") {
        requests.push({ id: `req-${requests.length + 1}`, query: entry });
      } else if (entry && typeof entry.query === "string" && entry.query.length > 0) {
        requests.push({ id: String(entry.id ?? `req-${requests.length + 1}`), query: entry.query });
      } else {
        throw new Error(`queries file entry ${index} must be a string or an {id, query} object`);
      }
    }
  }
  args.queries.forEach((query, index) => {
    requests.push({ id: args.requestIds[index] ?? `req-${requests.length + 1}`, query });
  });
  if (requests.length === 0 && env.PORTAL_QUERY) {
    requests.push({ id: env.PORTAL_REQUEST_ID || "req-1", query: env.PORTAL_QUERY });
  }
  if (requests.length === 0 && args.dryRun) {
    requests.push({ id: "req-1", query: "SAMPLE-PART" });
  }
  return requests;
}

function validateEnv(env) {
  const missing = REQUIRED_ENV.filter((key) => !env[key]);
  if (missing.length > 0) {
    // Names only: configuration values must never appear in errors.
    throw new Error(`missing portal configuration: ${missing.join(", ")}`);
  }
}

async function main(argv, env) {
  const args = parseArgs(argv);
  if (args.help) {
    console.log(USAGE);
    return 0;
  }

  // Resolve the work and validate configuration before acquiring any
  // resource, so a usage error can never leak a browser or a session.
  const requests = await loadRequests(args, env);
  if (requests.length === 0) {
    throw new Error("no queries provided: use --query, --queries-file or PORTAL_QUERY");
  }
  const credentials = new Credentials(env.PORTAL_USERNAME, env.PORTAL_PASSWORD);
  const secrets = credentials.secrets;
  const log = (line) => process.stderr.write(`${redact(line, secrets)}\n`);

  let pool = null;
  let browser = null;
  const shutdown = () => {
    pool?.close().catch(() => {});
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
  try {
    let driverFactory;
    if (args.dryRun) {
      ({ factory: driverFactory } = createFakeDriverFactory({ credentials }));
      log("dry-run: executing against fake driver, no browser started");
    } else {
      validateEnv(env);
      const { chromium } = await import("playwright");
      const { createPlaywrightDriverFactory } = await import("./portal_flow.mjs");
      browser = await chromium.launch({ headless: true });
      driverFactory = createPlaywrightDriverFactory(browser, { url: env.PORTAL_URL, credentials });
    }

    pool = new SessionPool({
      driverFactory,
      size: args.poolSize ?? (Number(env.PORTAL_POOL_SIZE) || 2),
      queryTimeoutMs: args.queryTimeoutMs ?? (Number(env.PORTAL_QUERY_TIMEOUT_MS) || 15_000),
      secrets,
      logger: log,
    });

    // Envelopes are built in the submitting closure, so every line is
    // attributed to its requestId even if an unexpected error slips through.
    const envelopes = await Promise.all(
      requests.map((request) =>
        pool.submit(request).then(
          (data) => ({ requestId: request.id, status: "ok", data }),
          (error) => ({
            requestId: request.id,
            status: "error",
            error: {
              code: error?.code ?? "ERROR",
              message: redact(error?.message ?? String(error), secrets),
            },
          }),
        ),
      ),
    );
    for (const envelope of envelopes) console.log(JSON.stringify(envelope));
    return envelopes.every((envelope) => envelope.status === "ok") ? 0 : 1;
  } finally {
    process.off("SIGINT", shutdown);
    process.off("SIGTERM", shutdown);
    if (pool) await pool.close();
    if (browser) await browser.close().catch(() => {});
  }
}

const isEntryPoint = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isEntryPoint) {
  main(process.argv.slice(2), process.env).then(
    (code) => {
      process.exitCode = code;
    },
    (error) => {
      // Last-resort handler: redact against the process env so a failure can
      // never echo configured credentials.
      const secrets = [process.env.PORTAL_USERNAME, process.env.PORTAL_PASSWORD].filter(Boolean);
      process.stderr.write(`${redact(error?.message ?? String(error), secrets)}\n`);
      process.exitCode = 2;
    },
  );
}
