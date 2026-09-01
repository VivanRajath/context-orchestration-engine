/* What the run produced, and the means to check it.

   The page spends a lot of words telling you the work carried through. This
   is where it stops telling you. Every item below is traced to the worker
   that produced it and to the exact briefing that worker was given, and the
   whole record is here in the form the engine actually wrote, to copy or to
   download. Nothing is summarised for effect.

   It is assembled from the event stream rather than fetched afterwards. On a
   host that freezes an instance the moment a response ends, a second request
   for the same run can land somewhere that has never heard of it, so the run
   has to carry everything it wants to show while it is still speaking. */

import { $, esc } from "./dom.js";

// One run's worth of evidence. Cleared when the next one starts.
let RUN = null;

function empty() {
  return { started: null, workers: [], finished: null };
}

function worker(id) {
  let found = RUN.workers.find(function (w) { return w.worker_id === id; });
  if (!found) {
    found = { worker_id: id, briefing: null, result: null, report: null, verdict: null };
    RUN.workers.push(found);
  }
  return found;
}

export function builtReset() {
  RUN = empty();
  $("builtCard").hidden = true;
  $("paneBuilt").innerHTML = "";
  $("paneTrace").innerHTML = "";
  $("rawRecord").textContent = "";
}

/* Every event, kept whole. What gets shown is decided later; what gets kept
   is not, because an event that was thrown away cannot be checked. */
export function builtRecord(d) {
  if (!RUN) { RUN = empty(); }
  if (d.event === "run_started") {
    RUN.started = d;
  } else if (d.event === "worker_started") {
    const w = worker(d.worker_id);
    w.seq = d.seq;
    w.model = d.model;
    w.vendor = d.vendor;
    w.task = d.task;
    w.briefing = d.package;
  } else if (d.event === "worker_completed") {
    const w = worker(d.worker_id);
    w.result = d.result;
    w.report = d.report;
    w.duration_ms = d.duration_ms;
    w.context_tokens_in = d.context_tokens_in;
    w.raw_conversation_transferred = d.raw_conversation_transferred;
  } else if (d.event === "worker_failed") {
    worker(d.worker_id).error = d.error;
  } else if (d.event === "reconciled") {
    worker(d.worker_id).verdict = {
      summary: d.summary,
      accepted: d.accepted,
      duplicates_skipped: d.duplicates_skipped,
      warnings: d.warnings,
      unverified_artifacts: d.unverified_artifacts,
      rejected_resolutions: d.rejected_resolutions
    };
  } else if (d.event === "run_finished") {
    RUN.finished = d;
  }
}

// -- the work --------------------------------------------------------------

function group(name, count, note, inner) {
  return '<div class="bt-group"><div class="bt-head"><span class="label">' + esc(name) +
    '</span><span class="bt-count">' + count + "</span></div>" +
    (note ? '<p class="bt-note">' + note + "</p>" : "") +
    '<div class="bt-body">' +
    (inner || '<span class="none">Nothing of this kind was produced.</span>') +
    "</div></div>";
}

function provenance(who, rest) {
  return '<span class="who">' + esc(who) + (rest || "") + "</span>";
}

function paneBuilt() {
  const st = RUN.finished.state;
  const artifacts = st.artifacts || [];
  const decisions = st.decisions || [];
  const done = st.completed_tasks || [];
  const unverified = artifacts.filter(function (a) { return !a.verified; }).length;

  return (
    group("Artifacts", artifacts.length,
      "Named by the workers. The engine records who created each one and who " +
      "changed it afterwards, and grades every one of them " +
      '<span class="mono">unverified</span>, because nothing here opens a file ' +
      "and checks what is inside it." +
      (unverified ? " All " + unverified + " below carry that label." : ""),
      artifacts.map(function (a) {
        return '<div class="bt-item"><span class="bt-what">' + esc(a.name) +
          ' <span class="mono ver">v' + a.version + "</span></span>" +
          provenance("created by " + a.by,
            (a.modified_by.length ? ", changed by " + esc(a.modified_by.join(", ")) : "") +
            (a.verified ? "" : ", unverified")) + "</div>";
      }).join("")) +

    group("Steps finished", done.length,
      "Written into the record only after the reconciler accepted the claim.",
      done.map(function (t) {
        return '<div class="bt-item okv"><span class="bt-what">' + esc(t) + "</span></div>";
      }).join("")) +

    group("Decisions, and why", decisions.length,
      "The reason is the part a summary throws away first, which is why it is " +
      "a field rather than prose.",
      decisions.map(function (x) {
        return '<div class="bt-item"><span class="bt-what">' + esc(x.decision) + "</span>" +
          (x.reason ? '<span class="why">' + esc(x.reason) + "</span>" : "") +
          provenance("decided by " + x.by) + "</div>";
      }).join(""))
  );
}

// -- where each item came from ---------------------------------------------

function fold(title, subtitle, body, open) {
  return '<details class="bt-fold"' + (open ? " open" : "") + "><summary>" +
    '<span class="bt-ft">' + title + "</span>" +
    '<span class="bt-fs">' + subtitle + "</span></summary>" +
    '<div class="bt-fb">' + body + "</div></details>";
}

function pre(label, text) {
  return '<div class="bt-pre"><span class="label">' + esc(label) + "</span><pre>" +
    esc(text) + "</pre></div>";
}

/* Counts keyed by kind, as the reconciler reports them: {decisions: 2, ...}.

   Kinds that scored nothing are dropped rather than printed as zeros: a line
   reading "0 issues, 0 artifacts, 1 assumptions" buries the one number in it
   and gets the grammar wrong on the way. */
function tally(counts) {
  return Object.keys(counts || {})
    .filter(function (k) { return counts[k]; })
    .map(function (k) {
      const noun = k.replace(/_/g, " ");
      return counts[k] + " " + (counts[k] === 1 ? noun.replace(/s$/, "") : noun);
    })
    .join(", ");
}

/* The step this whole design exists for: what a worker said, against what was
   allowed into the record. Rendered from the counts rather than a sentence,
   because the counts are what the engine actually wrote down. */
function verdict(v) {
  const kept = tally(v.accepted);
  const dupes = tally(v.duplicates_skipped);
  const warnings = v.warnings || [];
  const rejected = v.rejected_resolutions || [];
  const lines = [];

  lines.push("<b>What the reconciler let through:</b> " + (kept || "nothing new") + ".");
  if (dupes) {
    lines.push("Already in the record, so not written twice: " + dupes + ".");
  }
  if ((v.unverified_artifacts || []).length) {
    lines.push("Stored but unchecked: " + esc(v.unverified_artifacts.join(", ")) + ".");
  }
  if (!warnings.length && !rejected.length) {
    lines.push("Nothing it said was refused this time.");
  }

  let out = '<p class="bt-note">' + lines.join(" ") + "</p>";

  // Two different refusals, listed apart. A warning is a discrepancy between
  // what a worker returned and what it wrote to its successor. A rejected
  // resolution is a worker closing a problem nothing had checked. Merged into
  // one list they read as the same complaint made twice.
  out += refusals("Discrepancies the reconciler recorded", warnings);
  out += refusals("Problems it refused to close", rejected);
  return out;
}

function refusals(title, items) {
  if (!items.length) { return ""; }
  return '<p class="bt-note bad-head"><b class="bad">' + esc(title) + " (" + items.length +
    ')</b></p><div class="bt-body">' +
    items.map(function (x) {
      return '<div class="bt-item flag"><span class="bt-what">' + esc(x) + "</span></div>";
    }).join("") + "</div>";
}

function paneTrace() {
  return RUN.workers.map(function (w, i) {
    const brief = w.briefing || {};
    const parts = [];

    parts.push('<p class="bt-note">Its assignment: <b>' + esc(w.task || "not recorded") +
      "</b>. This is everything it was given. There is no conversation above it, " +
      "and no earlier turn attached to it: " +
      (brief.contains_raw_conversation
        ? '<b class="bad">this briefing did carry raw conversation</b>.'
        : "the transfer audit found no raw conversation in it.") + "</p>");

    if (brief.rendered_text) {
      parts.push(pre(
        "The briefing it was handed (" + brief.estimated_tokens + " of a " +
        brief.token_budget + " token budget)", brief.rendered_text));
    }
    if (brief.omitted_sections && brief.omitted_sections.length) {
      parts.push('<p class="bt-note">Left out to fit the budget: ' +
        esc(brief.omitted_sections.join(", ")) +
        ". The briefing says so itself, so the worker knows what it was not told.</p>");
    }
    if (w.result) {
      parts.push(pre("What it claimed afterwards", JSON.stringify(w.result, null, 2)));
    }
    if (w.report) {
      parts.push(pre("What it told whoever came next", JSON.stringify(w.report, null, 2)));
    }
    if (w.verdict) {
      parts.push(verdict(w.verdict));
    }
    if (w.error) {
      parts.push('<div class="err">' + esc(w.error) + "</div>");
    }

    return fold(
      "Worker " + (w.seq != null ? w.seq : i + 1) + ": " + esc(w.worker_id),
      esc(w.model || "") + (w.vendor ? " &middot; " + esc(w.vendor) : "") +
        (w.context_tokens_in != null ? " &middot; " + w.context_tokens_in + " tokens in" : ""),
      parts.join(""), i === 0);
  }).join("");
}

// -- the record itself -----------------------------------------------------

function record() {
  return {
    task_id: RUN.started ? RUN.started.task_id : null,
    objective: RUN.started ? RUN.started.objective : null,
    stand_in: RUN.started ? RUN.started.mock : null,
    roster: RUN.started ? RUN.started.roster : [],
    workers: RUN.workers,
    final_state: RUN.finished ? RUN.finished.state : null,
    summary: RUN.finished ? RUN.finished.summary : null,
    continuity_maintained: RUN.finished ? RUN.finished.continuity : null
  };
}

function filename() {
  const id = (RUN.started && RUN.started.task_id) || "run";
  return "coe-" + String(id).replace(/[^A-Za-z0-9_-]/g, "") + ".json";
}

export function initBuilt() {
  const tabs = [
    ["btTabWork", "paneBuilt"],
    ["btTabTrace", "paneTrace"],
    ["btTabRaw", "paneRaw"]
  ];
  tabs.forEach(function (pair) {
    $(pair[0]).addEventListener("click", function () {
      tabs.forEach(function (other) {
        const on = other[0] === pair[0];
        $(other[0]).classList.toggle("on", on);
        $(other[0]).setAttribute("aria-selected", on ? "true" : "false");
        $(other[1]).hidden = !on;
      });
    });
  });

  $("btCopy").addEventListener("click", function () {
    const button = this;
    navigator.clipboard.writeText($("rawRecord").textContent).then(function () {
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = "Copy it"; }, 1600);
    }, function () {
      button.textContent = "Select it and copy";
    });
  });

  $("btDownload").addEventListener("click", function () {
    const blob = new Blob([$("rawRecord").textContent], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename();
    link.click();
    URL.revokeObjectURL(url);
  });
}

/* Called once the run has finished, because a half-finished record is not
   something anyone should be invited to check. */
export function builtShow() {
  if (!RUN || !RUN.finished) { return; }
  const s = RUN.finished.summary;
  const st = RUN.finished.state;

  $("builtStatus").textContent =
    (st.artifacts || []).length + " artifacts, " +
    (st.completed_tasks || []).length + " steps, " +
    RUN.workers.length + " workers";

  $("paneBuilt").innerHTML = paneBuilt();
  $("paneTrace").innerHTML = paneTrace();

  const text = JSON.stringify(record(), null, 2);
  $("rawRecord").textContent = text;
  $("btMeta").textContent =
    text.length.toLocaleString() + " characters, " +
    s.structured_handoffs + " handovers, " +
    s.raw_conversation_transfers + " conversations passed between workers";

  $("builtCard").hidden = false;
}
