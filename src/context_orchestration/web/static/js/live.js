/* What the engine is doing right now: the one-line status, the worker
   strip, and the six stages a turn passes through. Driven entirely by the
   event stream, so it holds no setup state of its own. */

import { $, esc } from "./dom.js";

// Which turn the run is on. Read by the worker strip, and by nothing
// outside this module.
let CUR_SEQ = 0;

function lvSay(what, why, detail) {
  $("lvWhat").textContent = what;
  $("lvWhy").textContent = why || "";
  $("lvDetail").textContent = detail || "";
}

function lvReset() {
  seqBuild();
  seqReset("not started");
  lvSay("Nothing running yet", "", "");
  $("wrail").innerHTML = "";
}

function buildWRail(roster) {
  $("wrail").innerHTML = roster.map(function (a, i) {
    return (i ? '<span class="wd-link"></span>' : "") +
      '<span class="wd" data-seq="' + a.seq + '" title="' + esc(a.task) + '">' +
      '<span class="bullet"></span>' + esc(a.worker_id) + "</span>";
  }).join("");
}

function wMark(seq, cls) {
  var el = $("wrail").querySelector('[data-seq="' + seq + '"]');
  if (!el) { return; }
  el.classList.remove("active", "done", "failed");
  el.classList.add(cls);
}

function counts(obj) {
  return Object.keys(obj).filter(function (k) { return obj[k]; })
    .map(function (k) { return k.replace(/_/g, " ") + " " + obj[k]; }).join(", ");
}

// -- the stage list ------------------------------------------------
//
// The diagram shows where the engine is; this says what it is doing, in
// the order it does it, and leaves behind what each stage actually did on
// this turn. It is also the only half of the run view that survives a
// phone screen, where the diagram is too wide to draw legibly.

var SEQ_STAGES = [
  { k: "compile", t: "Write the briefing",
    d: "The engine reads the record and writes one short briefing, small enough to fit the limit you set." },
  { k: "handover", t: "Hand it over",
    d: "The briefing is all the worker gets. No conversation from anyone before it." },
  { k: "work", t: "The worker answers",
    d: "It says what it did and writes a note for whoever comes next. So far this is only what it says." },
  { k: "reconcile", t: "Check what it said",
    d: "Every claim is checked before anything is written down. Some get refused." },
  { k: "commit", t: "Write it down",
    d: "What survives goes into the record, with the name of the worker that produced it." },
  { k: "handoff", t: "Log the handover",
    d: "A line goes in the record saying what crossed to the next worker, and that no conversation did." }
];

var SEQ_LABEL = { now: "now", done: "done", flag: "refused" };

function seqBuild() {
  $("seqList").innerHTML = SEQ_STAGES.map(function (st, i) {
    return '<li class="sq" id="sq-' + st.k + '">' +
      '<span class="sq-n">' + (i + 1) + "</span>" +
      '<span class="sq-t">' + esc(st.t) + "</span>" +
      '<span class="sq-st">waiting</span>' +
      '<span class="sq-d">' + esc(st.d) + "</span>" +
      '<span class="sq-live"></span></li>';
  }).join("");
}

// "state" is null (waiting), "now", "done" or "flag". "live" is what the
// stage did on this turn; pass "" to clear it, or omit to leave it alone.
function seqSet(key, state, live) {
  var el = $("sq-" + key);
  if (!el) { return; }
  el.classList.remove("is-now", "is-done", "is-flag");
  if (state) { el.classList.add("is-" + state); }
  el.querySelector(".sq-st").textContent = SEQ_LABEL[state] || "waiting";
  if (live !== undefined) {
    el.querySelector(".sq-live").textContent = live || "";
    el.classList.toggle("has-live", !!live);
  }
}

function seqReset(turn) {
  SEQ_STAGES.forEach(function (st) { seqSet(st.k, null, ""); });
  if (turn !== undefined) { $("seqTurn").textContent = turn; }
}

function lvEvent(d) {
  if (d.event === "run_started") {
    buildWRail(d.roster);
    lvSay("Writing the first briefing",
      "the record holds only the objective and the plan so far",
      d.total + " turns queued");
    seqReset("turn 1 of " + d.total);
    seqSet("compile", "now");

  } else if (d.event === "worker_started") {
    CUR_SEQ = d.seq;
    wMark(d.seq, "active");
    lvSay(d.worker_id + " is working",
      "handed " + d.package.included_sections.length + " pieces of the record, and no conversation",
      "turn " + d.index + " of " + d.total);
    seqReset("turn " + d.index + " of " + d.total + " · " + d.worker_id);
    seqSet("compile", "done",
      d.package.estimated_tokens + " of " + d.package.token_budget + " tok, " +
      d.package.included_sections.length + " sections" +
      (d.package.dropped_items.length ? ", " + d.package.dropped_items.length + " dropped" : ""));
    seqSet("handover", "done",
      "to " + d.worker_id + " · " + d.model + " · conversation: none");
    seqSet("work", "now");

  } else if (d.event === "worker_completed") {
    wMark(CUR_SEQ, "done");
    lvSay(d.worker_id + " answered",
      "it said what it did and left a note for the next one. Nothing checked yet.",
      d.duration_ms + " ms, " + d.messages_sent + " messages");
    seqSet("work", "done", d.duration_ms + " ms, " + d.messages_sent + " messages sent");
    seqSet("reconcile", "now");

  } else if (d.event === "worker_failed") {
    wMark(CUR_SEQ, "failed");
    lvSay(d.worker_id + " failed",
      "that is written down too, and the next worker carries on from the record", "");
    seqSet("work", "flag", "no usable answer. The run carries on from the record.");

  } else if (d.event === "reconciled") {
    var flagged = d.warnings.length + d.rejected_resolutions.length;
    lvSay("Checking what " + d.worker_id + " said",
      flagged
        ? flagged + (flagged === 1 ? " claim was" : " claims were") + " not taken at face value"
        : "all of it went into the record, stamped with who said it",
      counts(d.accepted) || "nothing new");
    seqSet("reconcile", flagged ? "flag" : "done",
      flagged
        ? flagged + (flagged === 1 ? " claim was" : " claims were") + " not accepted"
        : "every claim accepted");
    seqSet("commit", "done", counts(d.accepted) || "nothing new to write");
    seqSet("handoff", "now");

  } else if (d.event === "handoff") {
    var a = d.audit;
    lvSay(a.previous_worker + " to " + a.next_worker,
      "the record crossed over. The conversation did not.",
      a.package_tokens + " tok in the next briefing");
    seqSet("handoff", "done", a.previous_worker + " → " + a.next_worker + ", " +
      a.package_tokens + " tok, 0 conversation");

  } else if (d.event === "run_finished") {
    var s = d.summary;
    lvSay(d.continuity ? "Done. The work carried all the way through." : "Finished, but some turns failed",
      s.workers_used + " workers, " + s.structured_handoffs + " handovers, " +
      s.raw_conversation_transfers + " conversations passed on",
      s.total_context_tokens + " tok sent in total");
    // The last turn has nobody to hand to, so no handoff event arrives and
    // that stage would otherwise sit on "now" for good.
    if ($("sq-handoff").classList.contains("is-now")) {
      seqSet("handoff", "done", "last turn, nobody left to hand to");
    }
    $("seqTurn").textContent = "all " + s.workers_used + " turns complete";

  } else if (d.event === "run_error") {
    lvSay("The run stopped", "", "");
    $("seqTurn").textContent = "stopped";
  }
}

export { lvReset, lvEvent };
