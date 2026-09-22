/*
 * describeHttpFailure(response) → Promise<string>
 *
 * Turn a failed fetch() into a sentence a person can act on. The AI and PDF
 * endpoints answer failures with JSON {"error": "..."} the server has already
 * worded — the rate limiter, the quota check, the auth guard — so the body is
 * read first and the status code is only the fallback.
 *
 * Shared by the Studio and AI Tools. Each used to carry its own copy, and the
 * copies had already drifted: only one of them knew what a 413 meant.
 */
(function (global) {
  'use strict';

  async function describeHttpFailure(response) {
    var payload = {};
    try {
      if ((response.headers.get('content-type') || '').indexOf('application/json') !== -1) {
        payload = await response.json();
      }
    } catch (_) {
      /* Empty or malformed body — the status is all we have. */
    }
    if (payload && payload.error) { return payload.error; }

    var status = response.status;
    if (status === 401) { return 'Your session expired. Please sign in again.'; }
    if (status === 402) { return "You've used all your generations — upgrade to keep going."; }
    if (status === 413) { return 'That file is too large. Try a smaller one.'; }
    if (status === 429) { return 'Too many requests in a row. Wait about a minute, then try again.'; }
    if (status >= 500) { return 'The server hit a problem. Please try again in a moment.'; }
    return 'Request failed (' + status + '). Please try again.';
  }

  global.describeHttpFailure = describeHttpFailure;
}(window));
