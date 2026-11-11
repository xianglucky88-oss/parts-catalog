import { SessionInvalidError } from "./errors.mjs";

/**
 * Playwright-backed session driver for the real supplier portal.
 *
 * Each driver owns one browser context (isolated cookies/storage), performs
 * a single login at creation, and then serves any number of queries on its
 * page. The pool recycles the driver whenever a query fails, so a context is
 * never reused after an error.
 *
 * Credentials are only used to fill the login form here; they are never
 * logged or embedded in errors (the pool additionally redacts every error
 * that crosses its boundary).
 */

export function createPlaywrightDriverFactory(browser, { url, credentials, navigationTimeoutMs = 30_000 }) {
  return async () => {
    const context = await browser.newContext();
    try {
      const page = await context.newPage();
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: navigationTimeoutMs });
      await page.getByLabel("Username").fill(credentials.username);
      await page.getByLabel("Password").fill(credentials.password);
      await page.getByRole("button", { name: /sign in|login/i }).click();
      await page.getByLabel("Part number").waitFor({ state: "visible", timeout: navigationTimeoutMs });
      return new PlaywrightSessionDriver(context, page);
    } catch (error) {
      await context.close().catch(() => {});
      throw error;
    }
  };
}

class PlaywrightSessionDriver {
  #context;
  #page;
  #closed = false;

  constructor(context, page) {
    this.#context = context;
    this.#page = page;
  }

  async runQuery(query) {
    if (this.#closed) throw new Error("driver is closed");
    if (await this.#loginFormVisible()) {
      throw new SessionInvalidError("portal session expired (login form is visible)");
    }
    await this.#page.getByLabel("Part number").fill(query);
    await this.#page.getByRole("button", { name: /search/i }).click();
    if (await this.#loginFormVisible()) {
      throw new SessionInvalidError("portal session expired (redirected to login)");
    }
    const cell = this.#page.locator("[data-part-number]").first();
    await cell.waitFor({ state: "attached" });
    return await cell.evaluate((node) => ({
      partNumber: node.dataset.partNumber,
      stockStatus: node.dataset.stockStatus ?? "unknown",
      price: node.dataset.price ?? null,
    }));
  }

  async close() {
    if (this.#closed) return;
    this.#closed = true;
    await this.#context.close().catch(() => {});
  }

  async #loginFormVisible() {
    return await this.#page
      .getByLabel("Username")
      .isVisible()
      .catch(() => false);
  }
}
