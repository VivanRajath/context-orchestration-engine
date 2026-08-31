/* A faithful port of the compiler's budget enforcement, so a reader can
   feel what dropping sections at a limit actually does. */

import { $, esc } from "./dom.js";
// The widget measures the same real run the walkthrough takes apart, which is
// what stops the two disagreeing about what the compiler does.
import { WT } from "./walkthrough-data.js";

// =====================================================================
// BUDGET WIDGET
//
// A faithful port of ContextCompiler's budget enforcement: the same
// priority order, the same greedy fill measured against the *rendered*
// package, the same line-by-line trim before a section is abandoned, the
// same two sections that get truncated rather than dropped, and the same
// token heuristic (HeuristicTokenEstimator: max(words, chars // 4)).
// =====================================================================

var ALWAYS_KEEP = ["assigned_task", "objective"];

function tok(text) {
  if (!text) { return 0; }
  var words = text.split(/\s+/).filter(Boolean).length;
  return Math.max(words, Math.floor((text.length + 3) / 4));
}
function renderSection(sec) {
  return sec.title + "\n\n" + sec.lines.join("\n") +
    (sec.truncated ? "\n(truncated to fit token budget)" : "");
}
function cost(list) {
  return tok(list.map(renderSection).join("\n\n"));
}

function compile(budget) {
  var included = [], report = [];
  WT.sections.slice().sort(function (a, b) { return a.priority - b.priority; })
    .forEach(function (sec) {
      var whole = { title: sec.title, lines: sec.lines.slice(), truncated: false };
      if (cost(included.concat([whole])) <= budget) {
        included.push(whole);
        report.push({ sec: sec, state: "kept", lines: sec.lines.length });
        return;
      }
      if (ALWAYS_KEEP.indexOf(sec.key) !== -1) {
        var text = sec.lines.join("\n");
        while (true) {
          whole.lines = [text + " ..."];
          whole.truncated = true;
          if (cost(included.concat([whole])) <= budget || text.length <= 24) { break; }
          text = text.slice(0, Math.floor(text.length * 0.8));
        }
        included.push(whole);
        report.push({ sec: sec, state: "trim", lines: sec.lines.length, note: "text cut to fit" });
        return;
      }
      var kept = [];
      for (var i = 0; i < sec.lines.length; i++) {
        var probe = { title: sec.title, lines: kept.concat([sec.lines[i]]), truncated: true };
        if (cost(included.concat([probe])) > budget) { break; }
        kept.push(sec.lines[i]);
      }
      if (kept.length) {
        included.push({ title: sec.title, lines: kept, truncated: kept.length < sec.lines.length });
        report.push({
          sec: sec,
          state: kept.length < sec.lines.length ? "trim" : "kept",
          lines: kept.length, of: sec.lines.length
        });
      } else {
        report.push({ sec: sec, state: "gone", lines: 0, of: sec.lines.length });
      }
    });
  return { tokens: cost(included), report: report };
}

function bwRender() {
  var budget = parseInt($("bwRange").value, 10);
  var out = compile(budget);
  var gone = out.report.filter(function (r) { return r.state === "gone"; });
  var trim = out.report.filter(function (r) { return r.state === "trim"; });

  $("bwRead").className = "bw-read" + (gone.length ? " over" : "");
  $("bwRead").innerHTML = "<b>" + out.tokens + "</b> / " + budget + " tok &middot; " +
    (out.report.length - gone.length) + " of " + out.report.length + " sections";

  $("bwList").innerHTML = out.report.map(function (r) {
    var pin = ALWAYS_KEEP.indexOf(r.sec.key) !== -1;
    var st = r.state === "gone" ? "dropped"
      : r.state === "trim" ? (r.note || r.lines + " of " + r.of + " lines")
      : r.lines + (r.lines === 1 ? " line" : " lines");
    return '<div class="bw-sec ' + r.state + (pin ? " pin" : "") + '">' +
      '<span class="p">' + r.sec.priority + "</span>" +
      '<span class="nm">' + esc(r.sec.key.replace(/_/g, " ")) + "</span>" +
      '<span class="st">' + esc(st) + "</span></div>";
  }).join("");

  var foot;
  if (!gone.length && !trim.length) {
    foot = "Everything fits. This is exactly what the engine sent worker three on the real run.";
  } else if (!gone.length) {
    foot = "Still complete, but " + trim.length + " section" + (trim.length > 1 ? "s are" : " is") +
      " being trimmed line by line. The compiler abandons a whole section only once trimming stops being enough.";
  } else {
    var names = gone.map(function (r) { return r.sec.key.replace(/_/g, " "); });
    foot = "Gone at this budget: " + names.join(", ") + ". " +
      (names.indexOf("failed attempts") !== -1
        ? "Worker four can now repeat an approach that already failed, which is the precise failure this engine exists to prevent."
        : "The assigned task and the objective survive at any budget; they are truncated rather than dropped.");
  }
  $("bwFoot").textContent = foot;
}

export function initBudget() {
  $("bwRange").addEventListener("input", bwRender);
  bwRender();
}
