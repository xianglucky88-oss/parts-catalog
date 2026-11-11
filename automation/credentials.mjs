/**
 * Credential handling for the portal execution layer.
 *
 * Credentials are wrapped in a container that never serializes its content,
 * and every log line or error message that crosses a layer boundary is passed
 * through redact() so account names and passwords cannot leak into logs or
 * error objects.
 */

export class Credentials {
  #username;
  #password;

  constructor(username, password) {
    this.#username = typeof username === "string" ? username : "";
    this.#password = typeof password === "string" ? password : "";
  }

  get username() {
    return this.#username;
  }

  get password() {
    return this.#password;
  }

  /** Non-empty secret values that must never appear in output. */
  get secrets() {
    return [this.#username, this.#password].filter((value) => value.length > 0);
  }

  toJSON() {
    return "[redacted]";
  }

  toString() {
    return "[redacted]";
  }

  [Symbol.for("nodejs.util.inspect.custom")]() {
    return "[redacted]";
  }
}

/**
 * Replace every occurrence of each secret in `text` with a fixed marker.
 * Safe for arbitrary input; empty secrets are ignored.
 */
export function redact(text, secrets = []) {
  let output = String(text);
  for (const secret of secrets) {
    if (!secret) continue;
    output = output.split(secret).join("***");
  }
  return output;
}

/**
 * Build a new Error whose message is guaranteed credential-free. The original
 * stack and `cause` chain are deliberately dropped: they may embed the raw
 * message (or driver internals) containing secrets.
 */
export function sanitizeError(error, secrets = []) {
  const message = redact(error?.message ?? String(error), secrets);
  const clean = new Error(message);
  clean.name = typeof error?.name === "string" ? error.name : "Error";
  if (error?.code) clean.code = error.code;
  return clean;
}
