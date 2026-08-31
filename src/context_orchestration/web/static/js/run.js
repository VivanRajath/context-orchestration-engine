/* Starting a run, carrying its events back, and drawing what they say.

   Two transports. Locally the server holds the run in a background thread and
   an EventSource watches it. On a host that freezes an instance between
   invocations, one request does both halves instead, because nothing there
   guarantees a second request reaches the instance holding the first. */

import { $, esc } from "./dom.js";
import { lvEvent, lvReset } from "./live.js";
import { isMock } from "./setup.js";

// Transport state. All of this was free inside the old IIFE, where any
// module could have written to it; here it is private to the transport.
let ES = null;
let RUNNING = false;
let ENDED = false;
let QUEUE = [];
let PUMPING = false;
let PACE = 0;
let PREV = {};

/* Whether a run is in flight, so the button can refuse a second one. */
export function isRunning() { return RUNNING; }

function resetRun(label) {
  RUNNING = true;
  PREV = {};
  HANDED = null;
  $("runBtn").disabled = true;
  $("runBtn").textContent = "Running";
  $("runBody").innerHTML = "";
  $("stateBody").innerHTML = "";
  $("wcAll").hidden = true;
  $("wcAll").textContent = "Open all";
  $("runStatus").textContent = label;
  $("stateStatus").textContent = "running";
  $("dmLive").classList.add("on");
  lvReset();

  if (ES) { ES.close(); }
  // A mock run empties its queue almost instantly. Rendering is what gets
  // paced here - the engine is untouched, and a live run is already slow
  // enough to watch, so it plays at whatever speed the providers answer.
  PACE = (isMock() && $("pace").checked) ? 700 : 0;
  QUEUE = [];
  PUMPING = false;
  ENDED = false;

}

// Local: the server holds the run in a background thread and an EventSource
// watches it.
function startRun(taskId) {
  resetRun(taskId);
  ES = new EventSource("/api/runs/" + taskId + "/stream");
  ES.onmessage = function (m) { enqueue(JSON.parse(m.data)); };
  ES.onerror = function () { if (RUNNING && !QUEUE.length) { finish(); } };
}

// Serverless: one request both starts the run and carries its events back,
// because nothing there guarantees a second request reaches the instance
// that holds the first. EventSource cannot POST, so the SSE frames are read
// off the response body by hand.
function streamRun(body) {
  resetRun("starting");
  fetch("/api/runs/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify(body)
  }).then(function (r) {
    if (!r.ok) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        throw new Error(j.detail || "Could not start the run.");
      });
    }
    var reader = r.body.getReader();
    var decoder = new TextDecoder();
    var buf = "";
    function read() {
      return reader.read().then(function (chunk) {
        if (chunk.done) {
          if (RUNNING && !ENDED && !QUEUE.length) { finish(); }
          return;
        }
        buf += decoder.decode(chunk.value, { stream: true });
        // A frame ends at a blank line; whatever trails the last one is a
        // partial frame and waits for the next chunk.
        var frames = buf.split("\n\n");
        buf = frames.pop();
        frames.forEach(function (frame) {
          var payload = frame.split("\n").filter(function (line) {
            return line.indexOf("data:") === 0;
          }).map(function (line) {
            return line.slice(5).trim();
          }).join("");
          if (!payload) { return; }   // a keepalive comment
          var d = JSON.parse(payload);
          if (d.event === "run_started" && d.task_id) {
            $("runStatus").textContent = d.task_id;
          }
          enqueue(d);
        });
        return read();
      });
    }
    return read();
  }).catch(function (e) {
    RUNNING = false;
    $("runBtn").disabled = false;
    $("runBtn").textContent = "Run it";
    $("setupErr").innerHTML = '<div class="err" style="margin-top:.7rem">' + esc(e.message) + "</div>";
  });
}

function enqueue(d) {
  if (ENDED) { return; }
  if (d.event === "stream_end") {
    // The server closes the stream here, and an EventSource whose stream
    // closes will reconnect on its own - which replays the entire run into
    // a queue that is still playing. Close it ourselves the moment the
    // transport is done, and ignore anything that arrives afterwards.
    ENDED = true;
    if (ES) { ES.close(); ES = null; }
  }
  if (!PACE) { handle(d); return; }
  QUEUE.push(d);
  pump();
}

function pump() {
  if (PUMPING || !QUEUE.length) { return; }
  PUMPING = true;
  handle(QUEUE.shift());
  setTimeout(function () { PUMPING = false; pump(); }, PACE);
}

function finish() {
  RUNNING = false;
  if (ES) { ES.close(); ES = null; }
  $("runBtn").disabled = false;
  $("runBtn").textContent = "Run it";
  $("stateStatus").textContent = "done";
}

function handle(d) {
  lvEvent(d);
  if (d.event === "worker_started") { onWorkerStarted(d); }
  else if (d.event === "worker_completed") { onWorkerCompleted(d); }
  else if (d.event === "worker_failed") { onWorkerFailed(d); }
  else if (d.event === "reconciled") { onReconciled(d); }
  else if (d.event === "handoff") { onHandoff(d); }
  else if (d.event === "run_finished") { onFinished(d); }
  else if (d.event === "run_error") { onError(d); }
  else if (d.event === "stream_end") { finish(); }
}

function highlightPkg(text) {
  return esc(text).replace(/^([A-Z][A-Z0-9 ()\/,.'-]{3,})$/gm, '<span class="h">$1</span>');
}

// The next worker's briefing is the document that crossed the boundary, so
// it is shown against the worker that produced it rather than only against
// the worker that received it. It does not exist yet when that worker
// finishes, so a slot is left and filled when the next turn starts.
var HANDED = null;

function fillHanded(d) {
  if (!HANDED) { return; }
  var p = d.package;
  HANDED.innerHTML =
    '<span class="label">Handed to ' + esc(d.worker_id) + "</span>" +
    '<div class="pkg-text">' + highlightPkg(p.rendered_text) + "</div>" +
    '<div class="handoff-note">' + p.estimated_tokens + " tokens, " +
    p.included_sections.length + " pieces of the record" +
    (d.vendor ? ", read by " + esc(d.vendor) : "") +
    ". This is the whole of what crossed. No conversation went with it.</div>";
  HANDED = null;
}

function closeHanded(reason) {
  if (!HANDED) { return; }
  HANDED.innerHTML = '<span class="label">Handed on</span>' +
    '<div class="handoff-note">' + esc(reason) + "</div>";
  HANDED = null;
}

function checklist(st) {
  var done = (st.completed_tasks || []).map(function (t) {
    return '<li><span class="tick">[x]</span><span>' + esc(t) + "</span></li>";
  });
  var todo = (st.pending_tasks || []).map(function (t) {
    return '<li class="todo"><span class="tick">[ ]</span><span>' + esc(t) + "</span></li>";
  });
  return '<div class="box checklist"><span class="label">Done so far, and still to do</span>' +
    (done.length + todo.length
      ? "<ul>" + done.join("") + todo.join("") + "</ul>"
      : '<span class="none">Nothing recorded yet.</span>') + "</div>";
}

function onWorkerStarted(d) {
  fillHanded(d);
  var p = d.package;
  var pct = Math.min(100, Math.round((p.estimated_tokens / p.token_budget) * 100));
  var card = document.createElement("div");
  card.className = "wc running";
  card.innerHTML =
    '<button type="button" class="wc-head" aria-expanded="true">' +
      '<span class="wc-seq">' + d.index + "</span>" +
      '<span class="wc-who"><span class="wc-id">' + esc(d.worker_id) +
        (d.vendor ? ' <span class="wc-vendor">on ' + esc(d.vendor) + "</span>" : "") + "</span>" +
      '<span class="wc-model">' + esc(d.model) + "</span></span>" +
      '<span class="wc-right">' +
        '<span class="wc-live"><span class="spinner"></span>' +
          '<span class="pill warn">WORKING</span></span>' +
        '<span class="wc-done"></span>' +
      "</span><span class=\"chev\"></span></button>" +
    '<div class="wc-body">' +
      '<div class="wc-task">' + esc(d.task) + "</div>" +
      "<div>" +
        '<div class="meter-bar"><div class="meter-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="meter-cap"><span>given ' + p.estimated_tokens +
          " tok of the " + p.token_budget + " allowed</span><span>" +
          p.included_sections.length + " pieces</span></div>" +
      "</div>" +
      '<div class="pkgbox"><span class="label">What this worker was given</span>' +
        '<div class="pkg-text">' + highlightPkg(p.rendered_text) + "</div></div>" +
      (p.omitted_sections.length
        ? '<div class="box flagged"><span class="label">Left out to fit</span><div style="font-size:12px">' +
          esc(p.omitted_sections.join(", ")) + "</div></div>"
        : "") +
      '<div class="slot"></div>' +
    "</div>";
  card.querySelector(".wc-head").addEventListener("click", function () {
    wcOpen(card, card.classList.contains("closed"));
  });
  $("runBody").appendChild(card);
  $("wcAll").hidden = false;
}

// "closed" rather than hidden: the body stays in the document so anything
// written into it later, like the document handed to the next worker, lands
// where it belongs whether the reader has it open or not.
function wcOpen(card, open) {
  card.classList.toggle("closed", !open);
  card.querySelector(".wc-body").hidden = !open;
  card.querySelector(".wc-head").setAttribute("aria-expanded", String(open));
}

function wcAllOpen(open) {
  $("runBody").querySelectorAll(".wc").forEach(function (c) { wcOpen(c, open); });
  $("wcAll").textContent = open ? "Close all" : "Open all";
}

/* Wiring, called once by main.js. Importing this module must not touch the
   page: that is what keeps the load order from mattering. */
export function initRunView() {
  $("wcAll").addEventListener("click", function () {
    var anyClosed = !!$("runBody").querySelector(".wc.closed");
    wcAllOpen(anyClosed);
  });
}

function currentCard() {
  var cards = $("runBody").querySelectorAll(".wc");
  return cards.length ? cards[cards.length - 1] : null;
}

function box(title, items) {
  return '<div class="box"><span class="label">' + title + "</span>" +
    (items.length
      ? "<ul>" + items.map(function (i) { return "<li>" + i + "</li>"; }).join("") + "</ul>"
      : '<span class="none">None.</span>') + "</div>";
}

function onWorkerCompleted(d) {
  var card = currentCard();
  if (!card) { return; }
  card.className = card.className.replace("running", "").trim();
  card.querySelector(".wc-live").innerHTML =
    '<span class="pill ok">' + d.duration_ms + " MS</span>" +
    '<span class="pill ok">' + d.messages_sent + " MESSAGES</span>";

  var r = d.result, rep = d.report;
  card.querySelector(".slot").innerHTML =
    '<div class="grid2">' +
      box("Decisions made", (r.decisions || []).map(function (x) {
        return esc(x.decision) + (x.reason ? '<span class="why">' + esc(x.reason) + "</span>" : "");
      })) +
      box("Artifacts", (r.artifacts || []).map(function (x) { return esc(x.name); })) +
      box("Problems raised", (r.issues || []).map(function (x) {
        return "[" + esc(x.severity) + "] " + esc(x.description);
      })) +
      box("Dead ends", (r.failed_attempts || []).map(function (x) {
        return esc(x.attempt) + (x.reason ? '<span class="why">' + esc(x.reason) + "</span>" : "");
      })) +
    "</div>" +
    '<div class="box" style="margin-top:.8rem"><span class="label">Note for the next worker</span>' +
      '<div style="font-size:12.5px">' + esc(rep.notes_for_next_worker || "None.") + "</div></div>";
}

function onWorkerFailed(d) {
  var card = currentCard();
  if (!card) { return; }
  // Keep whatever open/closed state it had, and stay open: a turn that
  // failed is the last thing worth hiding.
  card.className = "wc failed";
  wcOpen(card, true);
  card.querySelector(".wc-right").innerHTML = '<span class="pill bad">FAILED</span>';
  card.querySelector(".slot").innerHTML = '<div class="err">' + esc(d.error) + "</div>";
}

function onReconciled(d) {
  var card = currentCard();
  if (!card) { return; }
  var acc = Object.keys(d.accepted).filter(function (k) { return d.accepted[k]; })
    .map(function (k) { return k.replace(/_/g, " ") + " " + d.accepted[k]; }).join(", ");
  var dup = Object.keys(d.duplicates_skipped).filter(function (k) { return d.duplicates_skipped[k]; })
    .map(function (k) { return k.replace(/_/g, " ") + " " + d.duplicates_skipped[k]; }).join(", ");
  var flagged = d.warnings.length || d.unverified_artifacts.length || d.rejected_resolutions.length;

  card.querySelector(".slot").insertAdjacentHTML("beforeend",
    '<div class="box ' + (flagged ? "flagged" : "good") + '" style="margin-top:.8rem">' +
    '<span class="label">What was checked</span>' +
    '<div style="font-size:12.5px;margin-bottom:.35rem">Kept: ' + (acc || "nothing new") +
    (dup ? " (duplicates suppressed: " + esc(dup) + ")" : "") + "</div>" +
    (d.rejected_resolutions.length
      ? '<div style="font-size:12.5px;margin-bottom:.35rem"><b>Said to be fixed, and not accepted:</b> ' +
        esc(d.rejected_resolutions.join("; ")) + "</div>"
      : "") +
    (d.warnings.length
      ? "<ul>" + d.warnings.map(function (w) { return "<li>" + esc(w) + "</li>"; }).join("") + "</ul>"
      : "") + "</div>");

  card.querySelector(".slot").insertAdjacentHTML("beforeend",
    '<div class="after">' + checklist(d.state) +
    '<div class="box handdoc"><span class="label">Handed on</span>' +
    '<div class="handoff-note">Waiting for the next worker to start.</div></div></div>');
  HANDED = card.querySelector(".handdoc");

  // The turn is settled, so fold it away with its result on the header.
  if (!card.classList.contains("failed")) {
    card.classList.add("done");
    card.querySelector(".wc-done").innerHTML = wcSummary(d);
    wcOpen(card, false);
  }
  $("wcAll").textContent = "Open all";

  renderState(d.state);
}

// What a closed card says for itself: how far through the plan the run is,
// what went into the record, and whether anything was refused.
function wcSummary(d) {
  var st = d.state;
  var done = (st.completed_tasks || []).length;
  var total = done + (st.pending_tasks || []).length;
  var kept = Object.keys(d.accepted).reduce(function (a, k) { return a + d.accepted[k]; }, 0);
  var flagged = d.warnings.length + d.rejected_resolutions.length;
  return '<span class="pill ok">DONE</span>' +
    '<span class="wc-tag kept">' + done + " of " + total + " steps done</span>" +
    '<span class="wc-tag">' + (kept ? kept + " into the record" : "nothing new") + "</span>" +
    (flagged ? '<span class="wc-tag flag">' + flagged + " questioned</span>" : "");
}

function onHandoff(d) {
  var a = d.audit;
  var el = document.createElement("div");
  el.className = "boundary";
  el.innerHTML =
    "<span><b>" + esc(a.previous_worker) + "</b> to <b>" + esc(a.next_worker) + "</b></span>" +
    "<span>conversation passed on: <b>NO</b></span>" +
    "<span>the record: <b>YES</b></span>" +
    "<span>note to the next worker: <b>" + (a.handoff_report_included ? "YES" : "NO") + "</b></span>" +
    "<span>" + a.package_tokens + " tok</span>";
  $("runBody").appendChild(el);
}

function row(k, v) {
  return '<div class="sf-row"><span>' + k + "</span><b>" + esc(v) + "</b></div>";
}

function onFinished(d) {
  var s = d.summary;
  var el = document.createElement("div");
  el.className = "summary-final" + (d.continuity ? "" : " bad");
  el.innerHTML =
    '<span class="label" style="display:block;margin-bottom:.55rem">Finished</span>' +
    row("Workers that took a turn", s.workers_used) +
    row("Conversations passed between them", s.raw_conversation_transfers) +
    row("Handovers", s.structured_handoffs) +
    row("Briefings written", s.packages_compiled) +
    row("Words sent, counted in tokens", s.total_context_tokens) +
    row("Claims that were questioned", s.reconciliation_warnings) +
    row("Work carried all the way through", d.continuity ? "YES" : "NO") +
    (s.failures && s.failures.length
      ? '<div class="err" style="margin-top:.6rem">' + esc(s.failures.join(" | ")) + "</div>"
      : "");
  $("runBody").appendChild(el);
  // The last turn has nobody to hand to, so no further worker starts and the
  // open slot on the final card would wait for ever.
  closeHanded("Last turn. There was nobody left to hand to.");
  $("runBody").insertAdjacentHTML("beforeend", debrief(d));
  renderState(d.state);
  $("stateStatus").textContent = d.state.status;
  finish();
}

// Read after the run: four concrete things to scroll back and look at,
// written against what this particular run actually produced.
function debrief(d) {
  var st = d.state, s = d.summary;
  var unverified = (st.artifacts || []).filter(function (a) { return !a.verified; }).length;
  var openIssues = (st.issues || []).filter(function (i) { return !i.resolved; });
  var claimed = openIssues.filter(function (i) { return i.claimed_by; });
  var items = [];

  items.push("<b>Nobody read anybody else's conversation.</b> " +
    s.structured_handoffs + " handover" + (s.structured_handoffs === 1 ? "" : "s") +
    ", and <b>" + s.raw_conversation_transfers + "</b> conversations passed between them. " +
    "Each green line above is the written record of one handover. It is saved, not just shown here.");

  items.push("<b>Nothing grew out of control.</b> " + s.total_context_tokens +
    " tokens for the whole job, and every briefing stayed under the " + $("budget").value +
    " you set. Passing the conversation along instead would have grown at every turn, with no ceiling. " +
    "Look at what the last worker was given: it is a short briefing, not a chat log.");

  if (claimed.length) {
    items.push("<b>Something a worker said, that was not accepted.</b> \u201c" + esc(claimed[0].description) +
      "\u201d was declared resolved by " + esc(claimed[0].claimed_by) +
      ", and it is still open. Nothing checked it, so the claim was written down and the problem stayed on the list.");
  } else if (s.reconciliation_warnings) {
    items.push("<b>" + s.reconciliation_warnings +
      " thing" + (s.reconciliation_warnings === 1 ? " was" : "s were") + " questioned.</b> " +
      "Look for the red box on the cards above. That is something a worker said that was not taken at its word.");
  } else {
    items.push("<b>Nothing was questioned this time.</b> What the workers said matched what they actually handed back. " +
      "Run it again with a different plan and you will usually see at least one refused.");
  }

  items.push("<b>You can see the work carried through, not just be told it was.</b> The record holds " +
    (st.decisions || []).length + " decisions, " + (st.artifacts || []).length + " artifacts" +
    (unverified ? " (" + unverified + " of them unchecked)" : "") + ", " +
    (st.failed_attempts || []).length + " dead ends and " + openIssues.length +
    " open problem" + (openIssues.length === 1 ? "" : "s") +
    ". Look at who made each decision: the last worker was building on choices the first one made.");

  return '<div class="debrief"><span class="label">What just happened</span><ol>' +
    items.map(function (i) { return "<li>" + i + "</li>"; }).join("") +
    "</ol></div>";
}

function onError(d) {
  $("runBody").insertAdjacentHTML("beforeend", '<div class="err">' + esc(d.error) + "</div>");
  finish();
}

function renderState(st) {
  var groups = [
    ["Decisions", st.decisions, function (x) {
      return '<div class="st-item"><span>' + esc(x.decision) + "</span>" +
        (x.reason ? '<span class="why">' + esc(x.reason) + "</span>" : "") +
        '<span class="who">' + esc(x.by) + "</span></div>";
    }],
    ["Artifacts", st.artifacts, function (x) {
      return '<div class="st-item' + (x.verified ? " okv" : "") + '"><span>' + esc(x.name) +
        ' <span class="mono" style="font-size:10px;color:var(--slate)">v' + x.version + "</span></span>" +
        '<span class="who">' + esc(x.by) +
        (x.modified_by.length ? ", modified by " + esc(x.modified_by.join(", ")) : "") +
        (x.verified ? "" : ", unverified") + "</span></div>";
    }],
    ["Open issues", st.issues.filter(function (i) { return !i.resolved; }), function (x) {
      return '<div class="st-item' + (x.claimed_by ? " flag" : "") + '"><span>[' +
        esc(x.severity) + "] " + esc(x.description) + "</span>" +
        '<span class="who">' + esc(x.by) +
        (x.claimed_by ? ", resolution claimed by " + esc(x.claimed_by) + " and not accepted" : "") +
        "</span></div>";
    }],
    ["Failed attempts", st.failed_attempts, function (x) {
      return '<div class="st-item flag"><span>' + esc(x.attempt) + "</span>" +
        (x.reason ? '<span class="why">' + esc(x.reason) + "</span>" : "") +
        '<span class="who">' + esc(x.by) + "</span></div>";
    }],
    ["Completed work", st.completed_tasks.map(function (t) { return { t: t }; }), function (x) {
      return '<div class="st-item okv">' + esc(x.t) + "</div>";
    }],
    ["Assumptions", st.assumptions, function (x) {
      return '<div class="st-item"><span>' + esc(x.assumption) + "</span>" +
        '<span class="who">' + esc(x.by) + "</span></div>";
    }]
  ];

  $("stateBody").innerHTML = groups.map(function (g) {
    var name = g[0], items = g[1] || [], fmt = g[2];
    var grew = PREV[name] !== undefined && items.length > PREV[name];
    PREV[name] = items.length;
    return '<div class="st-group">' +
      '<div class="st-head"><span class="label">' + name + "</span>" +
      '<span class="st-count' + (grew ? " grew" : "") + '">' + items.length + "</span></div>" +
      '<div class="st-body">' +
      (items.length ? items.map(fmt).join("") : '<span class="none">Nothing yet.</span>') +
      "</div></div>";
  }).join("");
}

export { startRun, streamRun };
