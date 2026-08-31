/* Boot, and the wiring between the modules. The only file that knows about
   all of them, and the only one with side effects at import time. */

import { $, esc } from "./dom.js";
import { initTheme } from "./theme.js";
import { initWalkthrough } from "./walkthrough.js";
import { initBudget } from "./budget.js";
import { lvReset } from "./live.js";
import {
  initSetup, addKey, askForAPlan, buildRunBody, checkKeys, config,
  keysSave, planSteps, refreshPlan, renumber, setMode, setSource, stepRow,
  updateHeads, workersMissingAModel
} from "./setup.js";
import { initRunView, isRunning, startRun, streamRun } from "./run.js";

initTheme();
initWalkthrough();
initBudget();
initRunView();

// -- controls ---------------------------------------------------------

$("addStep").addEventListener("click", function () {
  $("steps").appendChild(stepRow("", "", ""));
  renumber();
  refreshPlan();
});

$("budget").addEventListener("input", function () {
  $("budgetVal").textContent = this.value + " tok";
  refreshPlan();
});

$("total").addEventListener("input", function () {
  $("totalVal").textContent = this.value + " tok";
  refreshPlan();
});

$("srcPool").addEventListener("click", function () {
  if (!this.disabled) { setSource("pool"); }
});
$("srcMine").addEventListener("click", function () {
  if (!this.disabled) { setSource("mine"); }
});
$("srcNone").addEventListener("click", function () { setSource("none"); });

$("modeOne").addEventListener("click", function () { setMode("one"); });
$("modeSeq").addEventListener("click", function () { setMode("seq"); });

$("addKey").addEventListener("click", addKey);
$("checkKeys").addEventListener("click", checkKeys);
$("keyRemember").addEventListener("change", keysSave);
$("planBtn").addEventListener("click", askForAPlan);

$("pgObjective").addEventListener("input", updateHeads);

// -- boot ---------------------------------------------------------------

fetch("/api/config").then((r) => r.json()).then((cfg) => {
  initSetup(cfg);
  lvReset();
  reanchor();
});

// Plan steps and auto-grown textareas change the page height after load, so a
// deep link like /#playground lands in the wrong place. Re-apply it once the
// content has settled.
function reanchor() {
  if (!location.hash || location.hash === "#") { return; }
  // A hash that is not a valid selector (a bare "#", or anything with
  // punctuation in it) makes querySelector throw, and this runs inside the
  // config fetch - an exception here would leave the playground half built.
  var target = null;
  try { target = document.querySelector(location.hash); } catch (e) { return; }
  if (!target) { return; }
  requestAnimationFrame(function () {
    requestAnimationFrame(function () {
      var prev = document.documentElement.style.scrollBehavior;
      document.documentElement.style.scrollBehavior = "auto";
      target.scrollIntoView();
      document.documentElement.style.scrollBehavior = prev;
    });
  });
}

$("runBtn").addEventListener("click", function () {
  if (isRunning()) { return; }

  const missing = workersMissingAModel();
  if (missing.length) {
    $("setupErr").innerHTML = '<div class="err" style="margin-top:.7rem">' +
      "Every worker needs a model before a real run: " + esc(missing.join(", ")) +
      ". Check a key first, or switch to the stand-in.</div>";
    return;
  }
  if (!planSteps().length) {
    $("setupErr").innerHTML = '<div class="err" style="margin-top:.7rem">' +
      "Add at least one step.</div>";
    return;
  }

  const body = buildRunBody();
  $("setupErr").innerHTML = "";

  // A host that freezes an instance between invocations cannot be asked to
  // hold a run across two requests. See run.js.
  const cfg = config();
  if (cfg && cfg.serverless) { streamRun(body); return; }

  fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  }).then((r) => r.json().then((j) => {
    if (!r.ok) { throw new Error(j.detail || "Could not start the run."); }
    return j;
  })).then((j) => startRun(j.task_id))
    .catch((e) => {
      $("setupErr").innerHTML =
        '<div class="err" style="margin-top:.7rem">' + esc(e.message) + "</div>";
    });
});
