import process from "node:process";
import { pathToFileURL } from "node:url";

const required = ["PORTAL_URL", "PORTAL_USERNAME", "PORTAL_PASSWORD", "PORTAL_QUERY"];

export function parseArgs(argv) {
  return { dryRun: argv.includes("--dry-run") };
}

export function configuration(env = process.env) {
  return Object.fromEntries(required.map((key) => [key, env[key] ?? ""]));
}

export function validate(config) {
  const missing = required.filter((key) => !config[key]);
  if (missing.length) throw new Error(`missing portal configuration: ${missing.join(", ")}`);
}

export async function runDry(config, log = console.log) {
  log(JSON.stringify({ mode: "dry-run", target: config.PORTAL_URL || "not-configured", queryPresent: Boolean(config.PORTAL_QUERY) }));
}

async function launchChromium() {
  const { chromium } = await import("playwright");
  return chromium.launch({ headless: true });
}

export async function runPortal(config, { launch = launchChromium, log = console.log } = {}) {
  const browser = await launch();
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
    log(JSON.stringify(result));
  } finally {
    await browser.close();
  }
}

export async function main(argv = process.argv.slice(2), env = process.env) {
  const config = configuration(env);
  const { dryRun } = parseArgs(argv);
  if (dryRun) await runDry(config);
  else {
    validate(config);
    await runPortal(config);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
