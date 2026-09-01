/* How many people have opened the page.

   The server owns the number; this asks for it. Two rules keep it honest:

   * one count per browser session, not per page load. Reloading, coming back
     through a link, and opening the playground in a second tab are the same
     person still reading;
   * the badge stays hidden unless the server says the total is durable. A
     store that resets when the host recycles an instance still reports a real
     number, just not the one the label claims, and a wrong number on this
     page of all pages is not worth having. */

import { $ } from "./dom.js";

const SEEN = "coe.counted";

export function initVisits() {
  const badge = $("visits");
  const value = $("visitsN");
  if (!badge || !value) { return; }

  // sessionStorage throws outright in some privacy modes, so every touch of
  // it is guarded and the fallback is simply to count the visit.
  let counted = false;
  try { counted = sessionStorage.getItem(SEEN) === "1"; } catch (e) { counted = false; }

  fetch("/api/visits", { method: counted ? "GET" : "POST" })
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (!counted) {
        try { sessionStorage.setItem(SEEN, "1"); } catch (e) { /* not essential */ }
      }
      if (!data || !data.durable) { return; }
      if (typeof data.views !== "number" || data.views < 1) { return; }
      value.textContent = data.views.toLocaleString();
      badge.hidden = false;
    })
    .catch(function () { /* a counter is not worth an error on the page */ });
}
