/* The seven-stage stepper: what happens inside one worker turn. */

import { $, esc } from "./dom.js";
import { WT } from "./walkthrough-data.js";

// =====================================================================
// WALKTHROUGH - one worker turn, taken apart
//
// Every value below was produced by an actual run of the demo task
// (mock gateway, 1600-token budget) and dumped straight out of the
// store, so this page cannot drift from what the engine does.
// =====================================================================

function pad(s, n) {
  s = String(s);
  while (s.length < n) { s += " "; }
  return s;
}

function fmtPay(text) {
  return esc(text)
    .replace(/^!! (.*)$/gm, '<span class="d">$1</span>')
    .replace(/^(#.*)$/gm, '<span class="c">$1</span>')
    .replace(/^([A-Z][A-Z0-9 ()\/,.'&+-]{3,})$/gm, '<span class="h">$1</span>');
}

function payCompile() {
  var s = WT.stateIn, out = "STATE AVAILABLE TO THE COMPILER\n\n";
  ["completed_tasks", "decisions", "artifacts", "open_issues", "failed_attempts", "assumptions"]
    .forEach(function (k) { out += pad(k, 20) + s[k] + "\n"; });
  out += "\nSECTIONS BUILT, IN PRIORITY ORDER\n\n";
  WT.sections.forEach(function (sec) {
    out += pad(String(sec.priority), 3) + pad(sec.key, 24) +
      sec.lines.length + (sec.lines.length === 1 ? " line" : " lines") + "\n";
  });
  out += "\n# every section fit inside " + WT.packageBudget + " tokens, so nothing was\n" +
    "# dropped on this turn. The next stage is what came out.";
  return out;
}

function paySend() {
  return "MESSAGE 1  role: system\n\n" +
    "You are an interchangeable worker in a multi-worker orchestration\n" +
    "engine. You did not participate in any earlier conversation.\n" +
    "Everything you know about this task is in the context package\n" +
    "below - it was compiled from the engine's canonical execution\n" +
    "state, not from a chat log.\n" +
    "  ... rules, then the JSON schema the answer must match\n\n" +
    "MESSAGE 2  role: user\n\n" + WT.packageText +
    "\n\n# that is the whole call. There is no message 3.";
}

function payWork() {
  return "WORKER RESULT  (validated against the WorkerResult schema)\n\n" +
    JSON.stringify(WT.result, null, 1) +
    "\n\n# a claim, not a fact. Nothing here has touched state yet.";
}

function payReport() {
  return "HANDOFF REPORT  (a second, separate model call)\n\n" +
    JSON.stringify(WT.report, null, 1);
}

function payReconcile() {
  var r = WT.reconcile, out = "ACCEPTED INTO CANONICAL STATE\n\n";
  Object.keys(r.accepted).forEach(function (k) {
    out += pad(k, 20) + r.accepted[k] + "\n";
  });
  out += "\nDUPLICATES SUPPRESSED\n\n";
  var dup = Object.keys(r.duplicates_skipped);
  out += dup.length
    ? dup.map(function (k) { return pad(k, 20) + r.duplicates_skipped[k]; }).join("\n") + "\n"
    : "none\n";
  if (r.warnings.length) {
    out += "\nDISCREPANCIES RECORDED\n\n";
    r.warnings.forEach(function (w) { out += "!! " + w + "\n"; });
  }
  if (r.unverified_artifacts.length) {
    out += "\n# " + r.unverified_artifacts.join(", ") + " was stored anyway, and stamped\n" +
      "# unverified. It travels forward carrying that mark.\n";
  }
  out += "\nPROVENANCE STAMPED BY THE ENGINE\n\n" +
    pad("recorded_by", 20) + "worker-3\n" +
    pad("recorded_at", 20) + "server clock\n" +
    pad("verified", 20) + "false\n\n" +
    "# the worker cannot write any of those three fields.";
  return out;
}

function payPersist() {
  return "SQLITE WRITES, BEFORE WORKER 4 IS ALLOWED TO START\n\n" +
    pad("state_snapshots", 20) + "seq 3, the full canonical state\n" +
    pad("worker_executions", 20) + "seq 3, worker-3, timings, verdict\n" +
    pad("handoffs", 20) + "seq 3, worker-3 to worker-4\n" +
    pad("context_packages", 20) + "seq 3, the " + WT.packageTokens + "-token package, verbatim\n" +
    pad("events", 20) + "worker_completed, context_handoff\n\n" +
    "# this is what makes a task resumable. Kill the process here and a fresh\n" +
    "# one compiles worker 4's package from these rows.\n" +
    "# No row holds an API key, a message array, or a transcript.";
}

function payHandoff() {
  var a = WT.audit;
  return "HANDOFF AUDIT  worker-3 to worker-4\n\n" +
    pad("raw_conversation_transferred", 32) + a.raw_conversation_transferred + "\n" +
    pad("canonical_state_transferred", 32) + a.canonical_state_transferred + "\n" +
    pad("handoff_report_included", 32) + a.handoff_report_included + "\n" +
    pad("package_tokens", 32) + a.package_tokens + " of " + a.token_budget + "\n\n" +
    "WORKER 4 STARTS FROM\n\n" +
    "a package compiled from scratch: " + WT.nextTokens + " tokens, " + WT.nextSections + " sections,\n" +
    "carrying worker-3's decisions, its unverified artifact, its open\n" +
    "refresh-token issue, and its note not to assume that issue is solved.\n\n" +
    "WORKER 4 NEVER RECEIVES\n\n" +
    "worker-3's prompt, worker-3's reasoning, worker-3's raw response, or\n" +
    "any message worker-1 and worker-2 ever exchanged.\n\n" +
    "# the loop closes. Worker 4 is now at stage 1, and cannot tell.";
}

var WT_STAGES = [
  {
    n: "Compile",
    title: "The engine builds a package out of state",
    body: "Worker three's turn starts from nothing at all. The compiler reads canonical state, scores every item against the task about to be assigned, anchors the founding decisions so recent detail cannot outrank them, and fills the token budget in strict priority order.",
    crosses: ["Structured state items, ranked and bounded"],
    blocked: "There is no conversation to include. The engine never stored one.",
    look: "In the playground: the meter on each worker card, and the section count beside it.",
    pay: payCompile, payLabel: "State to package", payMeta: "13 sections",
    nodes: ["wn-state", "wn-compiler"], arrows: ["wa-1"]
  },
  {
    n: "Send",
    title: "One system message, one package, nothing else",
    body: "This is the entire model call. The worker gets its instructions, and the compiled package as a single user message. The list is rebuilt from scratch every turn, so there is no mechanism by which an earlier worker's messages could ride along.",
    crosses: ["The rendered package text", "The worker's own system prompt"],
    blocked: "No prior messages, no session identity, no trace of the worker before it.",
    look: "In the playground: open \"Everything this worker received\" on any card. That is this, verbatim.",
    pay: paySend, payLabel: "The whole model call", payMeta: "2 messages",
    nodes: ["wn-package", "wn-worker"], arrows: ["wa-2", "wa-3"]
  },
  {
    n: "Work",
    title: "The worker answers in a schema",
    body: "A different model family, holding none of the earlier context, does the assigned task and returns a WorkerResult. Everything in it is a claim: decisions it made, artifacts it says it wrote, issues it hit, approaches that failed.",
    crosses: ["A validated WorkerResult object"],
    blocked: "Its reasoning is not captured, and is not wanted. Only recorded decisions outlive the turn.",
    look: "In the playground: the four boxes on a finished card are this object, unpacked.",
    pay: payWork, payLabel: "Worker result", payMeta: "untrusted claim",
    nodes: ["wn-worker", "wn-out"], arrows: ["wa-4"]
  },
  {
    n: "Report",
    title: "Then it writes to its successor",
    body: "A second, separate call. The worker is told its turn is over and asked what the next worker would otherwise have to rediscover. This is where the honest part goes: what is unfinished, what is uncertain, what must not be assumed.",
    crosses: ["A validated HandoffReport addressed to whoever runs next"],
    blocked: "The report is still a claim. Writing it down does not make it true.",
    look: "In the playground: \"Notes for the next worker\" at the foot of each card.",
    pay: payReport, payLabel: "Handoff report", payMeta: "for worker 4",
    nodes: ["wn-out"], arrows: []
  },
  {
    n: "Reconcile",
    title: "The trust boundary",
    body: "The reconciler is the only component allowed to write canonical state. It deduplicates against what is already there, stamps provenance itself, and records every discrepancy it noticed. Worker three named an artifact in prose that never appeared in its structured result, so the engine stored it and marked it unverified.",
    crosses: ["Accepted items, stamped with who recorded them and when"],
    blocked: "An issue is never closed on an unverified claim, and failed attempts are append only.",
    look: "In the playground: the Reconciler box on a card turns red when it refused something.",
    pay: payReconcile, payLabel: "Reconciler verdict", payMeta: "1 flagged",
    nodes: ["wn-reconciler"], arrows: ["wa-5"]
  },
  {
    n: "Persist",
    title: "Written to disk before anyone else runs",
    body: "State, execution, report and the exact package are committed after every turn rather than at the end of a run. From here the task is resumable by a different process, on a different machine, with a different set of workers configured.",
    crosses: ["Five rows in SQLite"],
    blocked: "No credential and no message array is ever written to the database.",
    look: "In the playground: nothing appears for this stage, which is the point. It happens in the gap between one card and the next.",
    pay: payPersist, payLabel: "Store writes", payMeta: "seq 3",
    nodes: ["wn-store", "wn-state"], arrows: ["wa-6", "wa-7"]
  },
  {
    n: "Hand off",
    title: "The next worker starts from state, not from history",
    body: "Worker four is handed a freshly compiled package and an assignment. It inherits every decision, the open issue, and the warning not to assume that issue is solved. It inherits none of the conversation, which is the entire point of the exercise.",
    crosses: ["A new package, compiled again from scratch"],
    blocked: "Zero raw conversation transfers, asserted by the run summary and by the tests.",
    look: "In the playground: the dashed green line between cards is this audit record.",
    pay: payHandoff, payLabel: "Handoff audit", payMeta: "worker 3 to 4",
    nodes: ["wn-next", "wn-state"], arrows: ["wa-8"]
  }
];

var WT_AT = 0;

function wtRender() {
  var s = WT_STAGES[WT_AT];

  $("wtRail").querySelectorAll(".wt-chip").forEach(function (c, i) {
    c.setAttribute("aria-selected", String(i === WT_AT));
    c.classList.toggle("done", i < WT_AT);
  });

  ["wn-state", "wn-compiler", "wn-package", "wn-worker", "wn-out", "wn-reconciler",
   "wn-store", "wn-next"].forEach(function (id) {
    $(id).classList.toggle("on", s.nodes.indexOf(id) !== -1);
  });
  for (var i = 1; i <= 8; i++) {
    $("wa-" + i).classList.toggle("on", s.arrows.indexOf("wa-" + i) !== -1);
  }

  $("wtStep").textContent = "Stage " + (WT_AT + 1) + " of " + WT_STAGES.length;
  $("wtTitle").textContent = s.title;
  $("wtBody").textContent = s.body;
  $("wtCross").innerHTML =
    s.crosses.map(function (c) {
      return '<div><span class="k">Crosses</span><span>' + esc(c) + "</span></div>";
    }).join("") +
    '<div><span class="k no">Does not</span><span>' + esc(s.blocked) + "</span></div>";
  $("wtLook").innerHTML = esc(s.look).replace(
    "In the playground:", '<a href="#playground">In the playground:</a>');
  $("wtPayLabel").textContent = s.payLabel;
  $("wtPayMeta").textContent = s.payMeta;
  $("wtPay").innerHTML = fmtPay(s.pay());
  $("wtPay").scrollTop = 0;
  $("wtPos").textContent = (WT_AT + 1) + " / " + WT_STAGES.length;
  $("wtPrev").disabled = WT_AT === 0;
  $("wtNext").textContent = WT_AT === WT_STAGES.length - 1 ? "Start over" : "Next →";
}

function wtGo(i) {
  WT_AT = (i + WT_STAGES.length) % WT_STAGES.length;
  wtRender();
}

export function initWalkthrough() {
  $("wtRail").innerHTML = WT_STAGES.map(function (s, i) {
    return '<button class="wt-chip" role="tab" aria-selected="false" data-i="' + i + '">' +
      '<span class="wn">' + (i + 1) + "</span><span>" + esc(s.n) + "</span></button>";
  }).join("");
  $("wtRail").querySelectorAll(".wt-chip").forEach(function (c) {
    c.addEventListener("click", function () { wtGo(parseInt(c.dataset.i, 10)); });
  });
  $("wtPrev").addEventListener("click", function () { wtGo(WT_AT - 1); });
  $("wtNext").addEventListener("click", function () { wtGo(WT_AT + 1); });

  // Arrow keys drive the stepper, but only while it is actually on screen
  // and nothing is being typed into.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") { return; }
    if (/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) { return; }
    var r = document.querySelector(".wt").getBoundingClientRect();
    if (r.bottom < 80 || r.top > window.innerHeight - 80) { return; }
    wtGo(WT_AT + (e.key === "ArrowRight" ? 1 : -1));
    e.preventDefault();
  });
  wtRender();
}
