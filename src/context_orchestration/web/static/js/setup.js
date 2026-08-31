/* Everything settled before a run: whose key, what the job is, who does
   which step. Owns that state and is the only module that writes it.

   A key typed here is sent with the run that uses it and attached to an
   in-memory worker config for that run only. It is never written to the
   database, never logged, and never echoed back. Keys the server lends never
   reach this page at all. */

import { $, esc, bindGrow } from "./dom.js";

// =====================================================================
// SETUP
//
// Four decisions, in the order the page asks for them: whose key, what the
// job is, who does which step, and go.
//
// A key typed here is sent with the run that uses it and attached to an
// in-memory worker config for that run only. It is never written to the
// database, never logged, and never echoed back. Keys we lend never reach
// this page at all - the server holds them and only says how many it has.
// =====================================================================

var CFG = null;          // /api/config
var SRC = "none";        // "pool" (borrow ours) | "mine" (pasted) | "none"
var MODE = "one";        // "one" (the engine plans) | "seq" (you plan)
var KEYS = [];           // [{ id, key, provider, api_base, report }]
var POOL = null;         // what a borrowed key can run
var TEAM = [];           // one worker per step
// Whether the stand-in is answering instead of a provider. Set by setSource,
// read through isMock() by anyone who needs it.
var MOCK = true;
var KEY_STORE = "coe-keys";
var PROV = [];           // the vendor catalogue, for the dropdowns

function keysSave() {
  try {
    if ($("keyRemember") && $("keyRemember").checked) {
      sessionStorage.setItem(KEY_STORE, JSON.stringify(
        KEYS.map(function (k) {
          return { id: k.id, key: k.key, provider: k.provider, api_base: k.api_base };
        })));
    } else {
      sessionStorage.removeItem(KEY_STORE);
    }
  } catch (e) {}
}

function keysLoad() {
  try {
    var raw = sessionStorage.getItem(KEY_STORE);
    if (!raw) { return false; }
    var rows = JSON.parse(raw);
    if (!rows || !rows.length) { return false; }
    KEYS = rows.map(function (k) {
      return { id: k.id, key: k.key || "", provider: k.provider || "",
               api_base: k.api_base || "", report: null };
    });
    return true;
  } catch (e) { return false; }
}

// -- which vendor is this? -------------------------------------------
//
// The same prefix table the server uses, so the vendor appears the moment
// you finish pasting rather than after a round trip. The server checks it
// again anyway; this is only so the page can say something immediately.

function detectLocal(key) {
  key = String(key || "").trim();
  if (!key) { return ""; }
  var best = "", len = 0;
  PROV.forEach(function (p) {
    (p.prefixes || []).forEach(function (pre) {
      if (key.indexOf(pre) === 0 && pre.length > len) { best = p.id; len = pre.length; }
    });
  });
  if (best) { return best; }
  return key.indexOf("sk-") === 0 ? "openai" : "";
}

function provLabel(id) {
  for (var i = 0; i < PROV.length; i++) { if (PROV[i].id === id) { return PROV[i].label; } }
  return "";
}

function needsBase(id) {
  for (var i = 0; i < PROV.length; i++) {
    if (PROV[i].id === id) { return !!PROV[i].needs_base_url; }
  }
  return false;
}

// -- the key cards ----------------------------------------------------

function newKey() {
  return { id: "k" + (KEYS.length + 1) + "-" + Math.random().toString(36).slice(2, 6),
           key: "", provider: "", api_base: "", report: null };
}

function keyVendor(k) { return k.provider || detectLocal(k.key); }

function renderKeys() {
  $("keyList").innerHTML = KEYS.map(function (k, i) {
    var pid = keyVendor(k);
    var known = !!pid;
    var opts = ['<option value="">Work it out for me</option>'].concat(
      PROV.map(function (p) {
        return '<option value="' + p.id + '"' + (k.provider === p.id ? " selected" : "") +
          ">" + esc(p.label) + "</option>";
      })).join("");
    var cls = k.report ? (k.report.ok ? " ok" : " bad") : "";
    return '<div class="kcard' + cls + '" data-i="' + i + '">' +
      '<div class="khead">' +
        '<span class="kname">Key ' + (i + 1) + "</span>" +
        '<span class="vendor' + (known ? " on" : "") + '">' +
          esc(known ? provLabel(pid) || pid : "unknown") + "</span>" +
        (KEYS.length > 1 ? '<button class="x kdrop" title="Remove">&times;</button>' : "") +
      "</div>" +
      '<div class="kwrap">' +
        '<input type="password" autocomplete="off" spellcheck="false" ' +
          'placeholder="paste the key" value="' + esc(k.key) + '"' +
          (k.key.trim() ? ' class="filled"' : "") + ">" +
        '<button class="eye" type="button">SHOW</button>' +
      "</div>" +
      '<div style="margin-top:.45rem"><select class="kprov">' + opts + "</select></div>" +
      (needsBase(k.provider)
        ? '<div class="kbase"><input type="text" class="kb" placeholder="https://your-endpoint/v1" ' +
          'value="' + esc(k.api_base) + '"></div>'
        : "") +
      keyMessage(k) +
      "</div>";
  }).join("");
  wireKeys();
}

function keyMessage(k) {
  if (!k.report) {
    return k.key.trim()
      ? '<div class="kmsg">Not checked yet.</div>'
      : '<div class="kmsg">Paste a key and we will ask its vendor what it can run.</div>';
  }
  var r = k.report;
  if (!r.ok) {
    return '<div class="kmsg bad">' + esc(r.error || "that key did not work") + "</div>";
  }
  return '<div class="kmsg ok"><b>' + esc(r.provider_label) + "</b> answered. " +
    r.models.length + (r.models.length === 1 ? " model" : " models") +
    " you can use here." + "</div>";
}

function wireKeys() {
  $("keyList").querySelectorAll(".kcard").forEach(function (card) {
    var k = KEYS[parseInt(card.dataset.i, 10)];
    var input = card.querySelector('input[type=password]');
    input.addEventListener("input", function () {
      k.key = input.value;
      k.report = null;
      input.classList.toggle("filled", !!k.key.trim());
      card.querySelector(".vendor").textContent =
        provLabel(keyVendor(k)) || (k.key.trim() ? "unknown" : "unknown");
      card.querySelector(".vendor").classList.toggle("on", !!keyVendor(k));
      keysSave();
      updateHeads();
    });
    card.querySelector(".eye").addEventListener("click", function (e) {
      var b = e.currentTarget;
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      b.textContent = show ? "HIDE" : "SHOW";
    });
    card.querySelector(".kprov").addEventListener("change", function (e) {
      k.provider = e.currentTarget.value;
      k.report = null;
      keysSave();
      renderKeys();
      updateHeads();
    });
    var base = card.querySelector(".kb");
    if (base) {
      base.addEventListener("input", function () { k.api_base = base.value; keysSave(); });
    }
    var drop = card.querySelector(".kdrop");
    if (drop) {
      drop.addEventListener("click", function () {
        KEYS = KEYS.filter(function (o) { return o !== k; });
        renderKeys();
        refreshPlan();
      });
    }
  });
}

// -- asking a vendor what a key can run -------------------------------

function currentRoles() {
  var roles = planSteps().map(function (st) { return st.role || ""; });
  return roles.length ? roles : ["architecture"];
}

function inspect(payload) {
  return fetch("/api/keys/inspect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }).then(function (r) {
    return r.json().then(function (j) {
      if (!r.ok) { throw new Error(j.detail || "Could not check that key."); }
      return j;
    });
  });
}

function checkKeys() {
  var pending = KEYS.filter(function (k) { return k.key.trim(); });
  if (!pending.length) {
    $("keyList").insertAdjacentHTML("beforeend",
      '<div class="kmsg bad">Paste at least one key first.</div>');
    return;
  }
  var btn = $("checkKeys");
  btn.disabled = true;
  btn.textContent = "Asking the vendors";
  var roles = currentRoles();
  Promise.all(pending.map(function (k) {
    return inspect({ key: k.key.trim(), provider: k.provider,
                     api_base: k.api_base || null, roles: roles })
      .then(function (j) { k.report = j; })
      .catch(function (e) { k.report = { ok: false, error: e.message, models: [] }; });
  })).then(function () {
    btn.disabled = false;
    btn.textContent = "Check the keys";
    renderKeys();
    refreshPlan();
  });
}

function loadPool() {
  if (!CFG.pool || !CFG.pool.available) { return Promise.resolve(); }
  return inspect({ use_pool: true, roles: currentRoles() })
    .then(function (j) { POOL = j; })
    .catch(function (e) { POOL = { ok: false, error: e.message, models: [] }; })
    .then(function () { renderPool(); });
}

function renderPool() {
  var pool = CFG.pool || {};
  if (!pool.available) {
    $("poolBox").innerHTML = '<div class="kmsg">This copy has no keys to lend. ' +
      'Paste one of your own, or run the stand-in.</div>';
    return;
  }
  var found = POOL && POOL.ok
    ? " We asked " + esc(POOL.provider_label) + " what they can run and got " +
      POOL.models.length + " usable models back."
    : POOL ? " " + esc(POOL.error || "They are not answering right now.") : "";
  $("poolBox").innerHTML =
    '<div class="lend"><b>No key? We have you covered.</b> ' +
    pool.count + " free-tier " + esc(pool.label) + " keys live on this server. " +
    "Borrow them and the run is real: real models, real answers, nothing to sign up for." +
    found +
    '<span class="sig">One key per worker, so no two workers share a credential or a ' +
    'quota. The keys stay on the server and are never sent to your browser.</span></div>';
}

// -- what a key offers, as a dropdown ---------------------------------

function reportFor(ref) {
  if (ref === "pool") { return POOL; }
  for (var i = 0; i < KEYS.length; i++) { if (KEYS[i].id === ref) { return KEYS[i].report; } }
  return null;
}

function usableKeys() {
  if (SRC === "pool") {
    return (POOL && POOL.ok) ? [{ ref: "pool", label: POOL.provider_label }] : [];
  }
  if (SRC === "mine") {
    return KEYS.filter(function (k) { return k.report && k.report.ok; })
      .map(function (k) { return { ref: k.id, label: k.report.provider_label }; });
  }
  return [];
}

function modelOptions(ref, chosen) {
  var r = reportFor(ref);
  if (!r || !r.ok) { return '<option value="">no models yet</option>'; }
  return r.models.map(function (m) {
    return '<option value="' + esc(m.id) + '"' + (m.id === chosen ? " selected" : "") + ">" +
      esc(m.label || m.id) + (m.note ? " \u00b7 " + esc(m.note) : "") + "</option>";
  }).join("");
}

// -- the plan ---------------------------------------------------------

function stepRow(task, role, budget) {
  var row = document.createElement("div");
  row.className = "step-row";
  row.innerHTML = '<span class="n"></span><textarea rows="2"></textarea>' +
    '<input class="cap" type="text" inputmode="numeric">' +
    '<button class="x" title="Remove this step">&times;</button>';
  var ta = row.querySelector("textarea");
  ta.value = task || "";
  row.dataset.role = role || "";
  var cap = row.querySelector(".cap");
  cap.value = budget || "";
  cap.style.display = MODE === "one" ? "" : "none";
  bindGrow(ta);
  ta.addEventListener("input", refreshPlan);
  cap.addEventListener("input", function () {
    // Once you have set one by hand, the slider stops overwriting it.
    row.dataset.manual = cap.value.trim() ? "1" : "";
    refreshPlan();
  });
  row.querySelector(".x").addEventListener("click", function () {
    row.remove(); renumber(); refreshPlan();
  });
  return row;
}

function renumber() {
  var rows = $("steps").querySelectorAll(".step-row");
  for (var i = 0; i < rows.length; i++) {
    rows[i].querySelector(".n").textContent = (i + 1) + ".";
  }
}

// Shares of the total, weighted so later workers get more: worker one is
// briefed on an empty record and has little to be told, the last inherits
// every decision before it. Mirrors split_budget on the server, so what is
// shown is what gets sent.
function splitTotal(total, n) {
  if (n <= 0) { return []; }
  if (n === 1) { return [Math.max(250, total)]; }
  var weights = [], i;
  for (i = 0; i < n; i++) { weights.push(1 + i / (n - 1)); }
  var sum = weights.reduce(function (a, b) { return a + b; }, 0);
  var shares = weights.map(function (w) {
    return Math.max(250, Math.round(w * total / sum / 50) * 50);
  });
  var over = shares.reduce(function (a, b) { return a + b; }, 0) - total;
  for (i = shares.length - 1; i >= 0 && over > 0; i--) {
    var take = Math.min(over, shares[i] - 250);
    if (take > 0) { shares[i] -= take; over -= take; }
  }
  return shares;
}

// Fill in every share the reader has not set by hand.
function applyCaps() {
  if (MODE !== "one") { return; }
  var rows = $("steps").querySelectorAll(".step-row");
  var shares = splitTotal(parseInt($("total").value, 10), rows.length);
  rows.forEach(function (row, i) {
    if (row.dataset.manual === "1") { return; }
    row.querySelector(".cap").value = shares[i] || "";
  });
}

function planSteps() {
  var out = [];
  $("steps").querySelectorAll(".step-row").forEach(function (row) {
    var task = row.querySelector("textarea").value.trim();
    if (!task) { return; }
    out.push({
      task: task,
      role: row.dataset.role || "",
      budget: parseInt(row.querySelector(".cap").value, 10) || 0
    });
  });
  return out;
}

function setSteps(steps) {
  $("steps").innerHTML = "";
  steps.forEach(function (st) {
    $("steps").appendChild(stepRow(st.task, st.role, st.budget));
  });
  renumber();
}

// A share the reader typed survives everything. A share that came from the
// planner is a starting point and moves with the slider again.
function clearManual() {
  $("steps").querySelectorAll(".step-row").forEach(function (row) {
    row.dataset.manual = "";
  });
}

function askForAPlan() {
  var objective = $("pgObjective").value.trim();
  if (!objective) {
    $("planned").style.display = "";
    $("planned").innerHTML = '<span class="who">nothing to plan</span>' +
      "<span>Say what you want done first.</span>";
    return;
  }
  var btn = $("planBtn");
  btn.disabled = true;
  btn.textContent = "Working out the steps";

  var keys = usableKeys();
  var ref = keys.length ? keys[0].ref : null;
  var report = ref ? reportFor(ref) : null;
  var body = {
    objective: objective,
    steps: Math.max(2, planSteps().length || 5),
    total_budget: parseInt($("total").value, 10),
    mock: SRC === "none" || !report || !report.ok
  };
  if (ref === "pool") {
    body.use_pool = true;
  } else if (ref) {
    var k = KEYS.filter(function (o) { return o.id === ref; })[0];
    body.key = k.key.trim();
    body.api_base = k.api_base || null;
  }
  if (report && report.ok) {
    body.provider = report.provider;
    body.model = report.recommended;
  }

  fetch("/api/plan/split", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  }).then(function (r) {
    return r.json().then(function (j) {
      if (!r.ok) { throw new Error(j.detail || "Could not work out the steps."); }
      return j;
    });
  }).then(function (j) {
    setSteps(j.steps);
    clearManual();
    $("total").value = j.total_budget;
    $("totalVal").textContent = j.total_budget + " tok";
    $("planned").style.display = "";
    $("planned").innerHTML =
      '<span class="who">' + (j.planner === "model" ? "written by a model" : "from a template") +
      "</span><span>" +
      (j.planner === "model"
        ? esc(String(j.model || "").replace(/^[^/]+\//, "")) + " split this into " +
          j.steps.length + " steps and divided the " + j.total_budget + " tokens between them."
        : "No model was available to plan with, so a template did it. " +
          j.total_budget + " tokens divided across " + j.steps.length + " steps.") +
      " Every step below is yours to edit.</span>" +
      (j.note ? '<span style="flex-basis:100%;color:var(--dropped)">' + esc(j.note) + "</span>" : "");
    refreshPlan();
  }).catch(function (e) {
    $("planned").style.display = "";
    $("planned").innerHTML = '<span class="who">could not plan</span><span>' +
      esc(e.message) + "</span>";
  }).then(function () {
    btn.disabled = false;
    btn.textContent = "Work out the steps";
  });
}

// -- the team ---------------------------------------------------------
//
// One worker per step. Where several keys are in play they are dealt out in
// turn, so consecutive workers are usually on different vendors - which is
// the arrangement the whole page exists to test.

function buildTeam() {
  var steps = planSteps();
  var keys = usableKeys();
  var previous = {};
  TEAM.forEach(function (w) { previous[w.id] = w; });

  TEAM = steps.map(function (st, i) {
    var id = "worker-" + (i + 1);
    var was = previous[id];
    var ref = keys.length ? keys[i % keys.length].ref : "";
    var report = reportFor(ref);
    var model = "";
    if (report && report.ok) {
      var offered = report.models.map(function (m) { return m.id; });
      // Keep a choice already made, if that key still offers it.
      if (was && was.keyRef === ref && offered.indexOf(was.model) !== -1) {
        model = was.model;
      } else {
        var spread = report.suggested || [];
        model = (spread[i % Math.max(1, spread.length)] || {}).model ||
          report.recommended || offered[0] || "";
      }
    }
    return { id: id, keyRef: ref, model: model, role: st.role, task: st.task,
             budget: st.budget };
  });
  renderTeam();
}

function renderTeam() {
  if (!TEAM.length) {
    $("team").innerHTML = '<div class="team-empty">Add a step above and a worker ' +
      "will appear here to take it.</div>";
    $("teamHd").textContent = "";
    return;
  }
  var keys = usableKeys();
  $("team").innerHTML = TEAM.map(function (w, i) {
    var keyOpts = keys.length
      ? keys.map(function (k) {
          return '<option value="' + esc(k.ref) + '"' + (k.ref === w.keyRef ? " selected" : "") +
            ">" + esc(k.ref === "pool" ? k.label + " (ours)" : k.label) + "</option>";
        }).join("")
      : '<option value="">stand-in</option>';
    return '<div class="trow" data-i="' + i + '">' +
      '<div class="tw"><span class="tid">' + esc(w.id) + "</span>" +
        '<span class="trole">' + esc(w.role || "step " + (i + 1)) + "</span></div>" +
      '<div class="ttask" title="' + esc(w.task) + '">' + esc(w.task) + "</div>" +
      '<select class="tkey"' + (keys.length ? "" : " disabled") + ">" + keyOpts + "</select>" +
      '<select class="tmodel"' + (keys.length ? "" : " disabled") + ">" +
        (keys.length ? modelOptions(w.keyRef, w.model)
                     : '<option value="">the stand-in</option>') + "</select>" +
      '<span class="tcap">' + (w.budget ? w.budget + " tok" : $("budget").value + " tok") +
      "</span></div>";
  }).join("");

  $("team").querySelectorAll(".trow").forEach(function (row) {
    var w = TEAM[parseInt(row.dataset.i, 10)];
    var keySel = row.querySelector(".tkey");
    var modelSel = row.querySelector(".tmodel");
    keySel.addEventListener("change", function () {
      w.keyRef = keySel.value;
      var r = reportFor(w.keyRef);
      w.model = (r && r.ok && r.recommended) || "";
      renderTeam();
    });
    modelSel.addEventListener("change", function () { w.model = modelSel.value; });
  });

  var vendors = {};
  TEAM.forEach(function (w) {
    var r = reportFor(w.keyRef);
    if (r && r.ok) { vendors[r.provider_label] = 1; }
  });
  var names = Object.keys(vendors);
  $("teamHd").textContent = SRC === "none"
    ? TEAM.length + " workers on the stand-in"
    : names.length
      ? TEAM.length + " workers, " + names.length +
        (names.length === 1 ? " vendor" : " vendors") + ": " + names.join(", ")
      : TEAM.length + " workers, no key yet";
}

// -- the summary above the run button ---------------------------------

function fact(k, v) {
  return '<div><span class="k">' + k + "</span><span>" + esc(v) + "</span></div>";
}

function updateRunPlan() {
  var steps = planSteps();
  var models = {};
  TEAM.forEach(function (w) { if (w.model) { models[w.model] = 1; } });
  var distinct = Object.keys(models).length;
  var total = MODE === "one"
    ? steps.reduce(function (a, st) { return a + (st.budget || 0); }, 0)
    : steps.length * parseInt($("budget").value, 10);

  $("runPlan").innerHTML =
    fact("Models", SRC === "none"
      ? "a stand-in, offline and free"
      : distinct
        ? distinct + (distinct === 1 ? " model" : " different models") +
          (SRC === "pool" ? ", on keys we lend you" : ", on your keys")
        : "none chosen yet") +
    fact("Turns", steps.length + (steps.length === 1 ? " worker turn" : " worker turns")) +
    fact("Ceiling", MODE === "one"
      ? total + " tokens for the whole job, divided"
      : $("budget").value + " tokens per briefing, " + total + " at most in total");
}

function updateHeads() {
  var pool = CFG && CFG.pool ? CFG.pool : {};
  if (SRC === "pool") {
    $("keyHd").textContent = pool.available
      ? (POOL && POOL.ok ? pool.count + " of ours, ready" : pool.count + " of ours")
      : "none to lend";
  } else if (SRC === "mine") {
    var ok = KEYS.filter(function (k) { return k.report && k.report.ok; }).length;
    var checked = KEYS.filter(function (k) { return k.report; }).length;
    var typed = KEYS.filter(function (k) { return k.key.trim(); }).length;
    $("keyHd").textContent = ok
      ? ok + " of " + KEYS.length + " working"
      : checked ? "none of them worked"
      : typed ? "not checked yet" : "nothing pasted";
  } else {
    $("keyHd").textContent = "not needed";
  }
  $("jobHd").textContent = MODE === "one" ? "the engine plans it" : "you plan it";
}

function refreshPlan() {
  applyCaps();
  buildTeam();
  updateRunPlan();
  updateHeads();
}

// -- switching ---------------------------------------------------------

function setSource(src) {
  SRC = src;
  MOCK = src === "none";
  $("srcPool").setAttribute("aria-pressed", String(src === "pool"));
  $("srcMine").setAttribute("aria-pressed", String(src === "mine"));
  $("srcNone").setAttribute("aria-pressed", String(src === "none"));
  $("poolBox").style.display = src === "pool" ? "" : "none";
  $("mineBox").style.display = src === "mine" ? "" : "none";
  $("noneBox").style.display = src === "none" ? "" : "none";
  $("srcHint").textContent =
    src === "pool" ? "Borrow a key from this server. Nothing to sign up for, nothing to pay."
    : src === "mine" ? "Any vendor, and as many as you like. One run may use several at once."
    : "No key at all. Everything runs except the model calls.";
  if (src === "mine" && !KEYS.length) { KEYS = [newKey()]; renderKeys(); }
  if (src === "pool" && CFG.pool && CFG.pool.available && !POOL) {
    loadPool().then(refreshPlan);
    return;
  }
  refreshPlan();
}

function setMode(mode) {
  MODE = mode;
  $("modeOne").setAttribute("aria-pressed", String(mode === "one"));
  $("modeSeq").setAttribute("aria-pressed", String(mode === "seq"));
  $("totalBox").style.display = mode === "one" ? "" : "none";
  $("budgetBox").style.display = mode === "one" ? "none" : "";
  $("modeHint").textContent = mode === "one"
    ? "Say what you want and the engine writes the steps, then divides the budget across them, giving later workers more because they inherit more. Everything it decides is editable before you run it."
    : "You write the steps and you set the ceiling. Every worker gets the same allowance, and the engine drops the least important material to stay inside it.";
  $("steps").querySelectorAll(".cap").forEach(function (c) {
    c.style.display = mode === "one" ? "" : "none";
  });
  refreshPlan();
}

/* The run request, assembled from everything above. Built here rather than
   in the transport, so the roster stays with the code that owns it. */
export function buildRunBody() {
  const steps = planSteps();
  const body = {
    objective: $("pgObjective").value.trim(),
    plan: steps.map((st) => st.task),
    mock: MOCK,
    mode: MODE === "one" ? "oneshot" : "sequential",
    budget: parseInt($("budget").value, 10),
    // One-shot divides a total, so every turn carries its own ceiling.
    step_budgets: MODE === "one" ? steps.map((st) => st.budget || 0) : [],
    workers: TEAM.map((w) => ({
      id: w.id, key_ref: w.keyRef || "pool", model: w.model, role: w.role
    })),
    // A borrowed key is named, never sent: the server holds it.
    keys: SRC === "mine"
      ? KEYS.filter((k) => k.key.trim()).map((k) => ({
          id: k.id, key: k.key.trim(), provider: k.provider,
          api_base: k.api_base || null
        }))
      : [],
    use_pool: SRC === "pool"
  };
  // The stand-in never touches a provider, so it is sent no roster and no key
  // at all: it runs against whatever workers.json configures.
  if (MOCK) { body.workers = []; body.keys = []; body.use_pool = false; }
  return body;
}

/* Which workers have no model chosen. Empty means the run may start. */
export function workersMissingAModel() {
  return MOCK ? [] : TEAM.filter((w) => !w.model).map((w) => w.id);
}

export function addKey() {
  KEYS.push(newKey());
  renderKeys();
  updateHeads();
}

export function isMock() { return MOCK; }
export function config() { return CFG; }

/* Everything the page needs before the first run: the demo task, the vendor
   catalogue, and which of the three key sources this deployment can offer. */
export function initSetup(cfg) {
  CFG = cfg;
  PROV = cfg.providers || [];

  $("pgObjective").value = cfg.demo.objective;
  bindGrow($("pgObjective"));
  setSteps(cfg.demo.plan.map((task) => ({ task: task, role: "", budget: 0 })));

  // A deployment with no keys of its own cannot lend one, and one that has
  // switched real calls off cannot take yours either. Say so on the control
  // rather than letting someone find out by pressing it.
  const lendable = !!(cfg.pool && cfg.pool.available) && cfg.live_enabled !== false;
  const byok = cfg.live_enabled !== false;
  $("srcPool").disabled = !lendable;
  $("srcMine").disabled = !byok;
  if (!byok) {
    $("srcPool").title = $("srcMine").title =
      cfg.live_disabled_reason || "Real model calls are switched off here.";
  } else if (!lendable) {
    $("srcPool").title = "This copy has no keys of its own to lend.";
  }

  if (keysLoad()) { $("keyRemember").checked = true; }
  renderKeys();
  setMode("one");
  setSource(lendable ? "pool" : byok && KEYS.length ? "mine" : "none");
}

export {
  planSteps, stepRow, renumber, refreshPlan,
  updateHeads, setSource, setMode, checkKeys, keysSave, askForAPlan
};
