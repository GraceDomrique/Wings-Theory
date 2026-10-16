/**
 * Forces a hard reload when this page is restored from the browser's
 * back/forward cache (bfcache) instead of a fresh navigation.
 *
 * Every page this script is loaded on (landing, console, POS, KDS) decides
 * what to show — the login form vs. the signed-in shell, which role's
 * session resumes — by reading localStorage once, at load time. bfcache
 * restoration skips that: it resurrects the exact DOM and JS state a page
 * was in the instant the browser navigated away, including a rendered
 * "Signed in as X — Continue" resume card, an unlocked POS/KDS/console
 * screen full of real order and inventory data, or in-memory state built
 * from a token that has since been signed out and revoked server-side
 * (wt/sessions.py). None of that is re-validated against the CURRENT
 * contents of localStorage or the server's session table on a bfcache
 * restore, because no script on the page runs again — the pageshow event
 * below is the only hook the platform gives for "this page came back from
 * bfcache, not a real load."
 *
 * Reloading forces every one of those pages to re-run its own normal
 * startup gate (read localStorage fresh, show login if there's no token or
 * the token's role is wrong) exactly as if the user had navigated to the
 * URL for the first time — which is also exactly what should happen after
 * an explicit sign-out followed by the browser Back button.
 */
(function () {
  'use strict';
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
      location.reload();
    }
  });
})();
