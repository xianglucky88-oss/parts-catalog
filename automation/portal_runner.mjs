import { randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import process from "node:process";

// Emits batches per contract/batch-envelope.schema.json. The collector never
// writes the business database; the Python importer consumes this envelope.
const CONTRACT_VERSION = "1.0";
const required = ["PORTAL_URL", "PORTAL_USERNAME", "PORTAL_PASSWORD", "PORTAL_QUERY", "PORTAL_SUPPLIER"];

function parseArgs(argv) {
  const args = { dryRun: false, out: null };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--dry-run") args.dryRun = true;
    else if (argv[i] === "--out") {
      args.out = argv[i + 1];
      i += 1;
    }
  }
  return args;
}

function configuration() {
  return Object.fromEntries(required.map((key) => [key, process.env[key] ?? ""]));
}

function validate(config) {
  const missing = required.filter((key) => !config[key]);
  if (missing.length) throw new Error(`missing portal configuration: ${missing.join(", ")}`);
}

function envelope(config, results, notes) {
  return {
    contract_version: CONTRACT_VERSION,
    batch_id: process.env.BATCH_ID ?? randomUUID(),
    supplier: config.PORTAL_SUPPLIER || "not-configured",
    queried_at: new Date().toISOString(),
    query: { part_number: config.PORTAL_QUERY || "not-configured" },
    results,
    ...(notes ? { notes } : {}),
  };
}

async function emit(payload, out) {
  const text = `${JSON.stringify(payload, null, 2)}\n`;
  if (out) await writeFile(out, text, "utf8");
  else process.stdout.write(text);
}

async function runDry(config, out) {
  // Simulation only: never touches the portal, always an explicitly empty batch.
  await emit(envelope(config, [], "dry-run simulation; no portal access performed"), out);
}

async function runPortal(config, out) {
  const { chromium } = await import("playwright");
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.goto(config.PORTAL_URL, { waitUntil: "domcontentloaded" });
    await page.getByLabel("Username").fill(config.PORTAL_USERNAME);
    await page.getByLabel("Password").fill(config.PORTAL_PASSWORD);
    await page.getByRole("button", { name: /sign in|login/i }).click();
    await page.getByLabel("Part number").fill(config.PORTAL_QUERY);
    await page.getByRole("button", { name: /search/i }).click();
    const results = [];
    for (const row of await page.locator("[data-part-number]").all()) {
      try {
        const part = await row.evaluate((node) => ({
          part_number: node.dataset.partNumber,
          brand: node.dataset.brand ?? "",
          description: node.dataset.description ?? "",
          unit: node.dataset.unit ?? "EA",
          package_quantity: node.dataset.packageQuantity ? Number(node.dataset.packageQuantity) : 1,
          lead_time_days: node.dataset.leadTimeDays ? Number(node.dataset.leadTimeDays) : null,
          stock_status: node.dataset.stockStatus ?? "unknown",
          price: node.dataset.price ?? null,
        }));
        if (!part.part_number) throw new Error("row is missing data-part-number");
        results.push({ status: "ok", part: { ...part, supplier: config.PORTAL_SUPPLIER } });
      } catch (error) {
        results.push({ status: "error", error: { code: "extract_failed", message: String(error?.message ?? error) } });
      }
    }
    await emit(envelope(config, results), out);
  } finally {
    await browser.close();
  }
}

const config = configuration();
const { dryRun, out } = parseArgs(process.argv.slice(2));
if (dryRun) await runDry(config, out);
else {
  validate(config);
  await runPortal(config, out);
}
