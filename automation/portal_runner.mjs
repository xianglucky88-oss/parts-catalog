import process from "node:process";

const required = ["PORTAL_URL", "PORTAL_USERNAME", "PORTAL_PASSWORD", "PORTAL_QUERY"];

function parseArgs(argv) {
  return { dryRun: argv.includes("--dry-run") };
}

function configuration() {
  return Object.fromEntries(required.map((key) => [key, process.env[key] ?? ""]));
}

function validate(config) {
  const missing = required.filter((key) => !config[key]);
  if (missing.length) throw new Error(`missing portal configuration: ${missing.join(", ")}`);
}

async function runDry(config) {
  console.log(JSON.stringify({ mode: "dry-run", target: config.PORTAL_URL || "not-configured", queryPresent: Boolean(config.PORTAL_QUERY) }));
}

async function runPortal(config) {
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
    const result = await page.locator("[data-part-number]").first().evaluate((node) => ({
      partNumber: node.dataset.partNumber,
      stockStatus: node.dataset.stockStatus ?? "unknown",
      price: node.dataset.price ?? null,
    }));
    console.log(JSON.stringify(result));
  } finally {
    await browser.close();
  }
}

const config = configuration();
const { dryRun } = parseArgs(process.argv.slice(2));
if (dryRun) await runDry(config);
else {
  validate(config);
  await runPortal(config);
}
