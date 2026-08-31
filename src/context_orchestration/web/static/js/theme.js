/* Page furniture: the light/dark switch, and keeping a glossary tooltip on
   screen. The saved theme is applied by an inline guard in <head> instead,
   because a module script is deferred and would flash the wrong one. */

import { $ } from "./dom.js";

export function initTheme() {

  $("themeBtn").addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme");
    var next = cur === "dark" ? "light" : cur === "light" ? "" : "dark";
    if (next) { document.documentElement.setAttribute("data-theme", next); }
    else { document.documentElement.removeAttribute("data-theme"); }
    try { localStorage.setItem("coe-theme", next); } catch (e) {}
  });

  // Glossary tooltips open to the right of the term. Near the right edge that
  // would run off the page, so flip them just before they are shown.
  document.querySelectorAll(".gloss").forEach(function (el) {
    function place() {
      var r = el.getBoundingClientRect();
      // clientWidth, not innerWidth - innerWidth includes the scrollbar.
      var avail = document.documentElement.clientWidth;
      var w = Math.min(352, avail - 40);
      el.classList.toggle("flip", r.left + w > avail - 24);
    }
    el.addEventListener("mouseenter", place);
    el.addEventListener("focus", place);
  });
}
