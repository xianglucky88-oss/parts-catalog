/**
 * Shared error types for the portal execution layer.
 *
 * Every failure that leaves the layer is a QueryError carrying the requestId
 * of the query it belongs to, so results can always be attributed to the
 * correct request even when sessions are recycled or the pool is closing.
 */

export class QueryError extends Error {
  /**
   * @param {string} requestId correlation id of the originating request
   * @param {string} code machine-readable failure code
   * @param {string} message sanitized, credential-free description
   */
  constructor(requestId, code, message) {
    super(message);
    this.name = "QueryError";
    this.requestId = requestId;
    this.code = code;
  }
}

/**
 * Raised by session drivers when the portal has dropped the authenticated
 * session (e.g. redirect back to the login form). The pool reacts by
 * recycling the session and retrying the query on a fresh one.
 */
export class SessionInvalidError extends Error {
  constructor(message = "portal session is no longer authenticated") {
    super(message);
    this.name = "SessionInvalidError";
    this.code = "SESSION_INVALID";
  }
}

export function isSessionInvalid(error) {
  return error instanceof SessionInvalidError || error?.code === "SESSION_INVALID";
}
