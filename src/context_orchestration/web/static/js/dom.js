/* The four helpers every other module needs, and nothing else. */

export const $ = (id) => document.getElementById(id);

/* Everything interpolated into markup goes through this. It is the only
   defence the page has, so it is one function rather than a habit. */
export const esc = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

function autogrow(el) {
  el.style.height = "auto";
  el.style.height = (el.scrollHeight + 2) + "px";
}

export function bindGrow(el) {
  el.addEventListener("input", function () { autogrow(el); });
  requestAnimationFrame(function () { autogrow(el); });
}
