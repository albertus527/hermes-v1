"use strict";
// R2.8.1 Human Calibration Reviewer — vanilla JS, no external deps.
// Server enforces blinding; this file never receives pre-lock answer keys.

const state = {
  csrf: null,
  enums: null,
  datasets: [],
  datasetId: null,
  order: [],          // sample ids in canonical order
  idx: 0,
  sample: null,       // current /api/sample payload
  version: 0,
  label: {},          // {category,direction,severity,ma_role}
  certainty: null,
  flagged: false,
  busy: false,
};

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (state.csrf && opts.method && opts.method !== "GET") {
    headers["X-CSRF-Token"] = state.csrf;
  }
  const res = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  let body = null;
  const ct = res.headers.get("Content-Type") || "";
  if (ct.includes("application/json")) body = await res.json();
  if (!res.ok) {
    const msg = (body && body.error) || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return body;
}

// ---- pairing ---------------------------------------------------------

async function boot() {
  try {
    const s = await api("/api/session");
    if (s.paired) {
      state.csrf = s.csrf_token;
      return startApp();
    }
  } catch (e) { /* fall through to gate */ }
  $("pairing").hidden = false;
}

async function doPair() {
  const code = $("paircode").value.trim();
  if (!code) return;
  try {
    const r = await api("/api/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    if (r.paired) {
      const s = await api("/api/session");
      state.csrf = s.csrf_token;
      $("pairing").hidden = true;
      return startApp();
    }
  } catch (e) {
    const m = $("pairmsg");
    m.textContent = e.message || "pairing failed";
    m.hidden = false;
  }
}

// ---- app init --------------------------------------------------------

async function startApp() {
  $("app").hidden = false;
  const ds = await api("/api/datasets");
  state.enums = ds.enums;
  state.datasets = ds.datasets;
  renderEnumOptions();
  await loadRubric();
  // Default to the canonical dataset (A).
  const canonical = state.datasets.find((d) => d.role === "CANONICAL")
    || state.datasets[0];
  await selectDataset(canonical.dataset_id);
}

function renderEnumOptions() {
  const mk = (host, field, values) => {
    const el = $(host);
    el.innerHTML = "";
    values.forEach((v) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "opt";
      b.textContent = v;
      b.dataset.value = v;
      b.dataset.field = field;
      b.addEventListener("click", () => setField(field, v));
      el.appendChild(b);
    });
  };
  mk("opts-category", "category", state.enums.category);
  mk("opts-direction", "direction", state.enums.direction);
  mk("opts-severity", "severity", state.enums.severity);
  mk("opts-ma_role", "ma_role", state.enums.ma_role);
  mk("opts-certainty", "certainty", ["CERTAIN", "UNSURE", "NEED_CONTEXT"]);
}

async function loadRubric() {
  const r = await api("/api/rubric");
  $("rubric-status").textContent = r.status;
  const host = $("rubric-body");
  host.innerHTML = "";
  const add = (title, text) => {
    const h = document.createElement("h3"); h.textContent = title;
    const p = document.createElement("p"); p.textContent = text;
    host.append(h, p);
  };
  add("Category", r.category);
  add("Entity / ticker relevance", r.entity_relevance);
  add("Direction", r.direction);
  add("Severity", r.severity);
  add("Confirmation & evidence", r.confirmation_and_evidence);
  add("M&A role", r.ma_role);
  add("OTHER vs company-specific", r.other_vs_specific);
  const h = document.createElement("h3"); h.textContent = "Unresolved (pending human approval)";
  const ul = document.createElement("ul");
  r.unresolved.forEach((u) => { const li = document.createElement("li"); li.textContent = u; ul.appendChild(li); });
  host.append(h, ul);
  await refreshRubricApproval();
}

async function refreshRubricApproval() {
  const p = await api(`/api/progress?dataset=${encodeURIComponent(state.datasetId || "A_CANONICAL_240")}`);
  // Approval state is surfaced via the sample payload; keep a lightweight check.
  const st = $("rubric-approve-state");
  st.textContent = "Rubric approval status is shown per sample. Approve to count locked labels canonically.";
}

async function selectDataset(datasetId) {
  state.datasetId = datasetId;
  // Per-dataset caches and navigation state must NEVER leak across datasets:
  // the ordered-id cache is dataset-scoped, so reset it before any load.
  state._orderedIds = null;
  state.idx = 0;
  const d = state.datasets.find((x) => x.dataset_id === datasetId);
  $("ds-name").textContent = d ? d.display_name : datasetId;
  $("ds-role").textContent = d ? d.role : "";
  await loadFirst();
}

async function loadFirst() {
  const r = await api(`/api/resume?dataset=${encodeURIComponent(state.datasetId)}`);
  state.idx = 0;
  if (r.sample_id) {
    await loadSampleById(r.sample_id);
  } else {
    await loadSampleAt(0);
  }
  await refreshProgress();
}

// The server is authoritative for order; we fetch by index using ordered ids.
async function loadSampleAt(index) {
  // Ask the server for the sample at a given position via resume + stepwise
  // navigation is avoided; instead we cache the ordered id list once.
  if (!state._orderedIds) await cacheOrder();
  const id = state._orderedIds[Math.min(Math.max(index, 0), state._orderedIds.length - 1)];
  state.idx = index;
  await loadSampleById(id);
}

async function cacheOrder() {
  // Derive order by requesting progress then walking sample ids via resume is
  // not exposed; use the export-free approach: fetch first, then rely on
  // next/prev by id from the server's ordered list.
  const ids = await api(`/api/order?dataset=${encodeURIComponent(state.datasetId)}`);
  state._orderedIds = ids.sample_ids;
}

async function loadSampleById(sampleId) {
  const s = await api(`/api/sample?dataset=${encodeURIComponent(state.datasetId)}&sample_id=${encodeURIComponent(sampleId)}`);
  state.sample = s;
  state.version = s.version;
  state.label = { ...s.draft };
  state.certainty = s.draft.certainty;
  state.flagged = s.draft.flagged;
  // Restore THIS sample's saved draft notes. The textarea is re-hydrated on
  // every sample load so a previous sample's notes (typed or stale) can
  // never bleed into a different headline; unsaved edits are intentionally
  // replaced by the server-confirmed draft (save first to keep them).
  $("notes").value = s.draft.notes || "";
  // Clear sample-specific transient state (revision reason belongs to the
  // sample it was typed for).
  $("revise-reason").value = "";
  if (!state._orderedIds) await cacheOrder();
  state.idx = state._orderedIds.indexOf(sampleId);
  renderSample();
}

// ---- rendering -------------------------------------------------------

function renderSample() {
  const b = state.sample.blind;
  $("s-ticker").textContent = b.ticker;
  $("s-time").textContent = b.published_at;
  $("s-source").textContent = b.source;
  $("s-headline").textContent = b.headline_text;
  $("pos-n").textContent = (state.idx + 1);
  $("pos-total").textContent = state._orderedIds.length;

  document.querySelectorAll(".opt[data-field]").forEach((el) => {
    const f = el.dataset.field;
    const val = f === "certainty" ? state.certainty : state.label[f];
    el.classList.toggle("selected", el.dataset.value === val);
  });

  const locked = state.sample.status === "LOCKED";
  const lockedMsg = locked
    ? "🔒 Locked. Use Reveal (optional) or Revise (requires a reason)."
    : "Not locked yet — drafts do not count toward §11.4.";
  $("statusline").textContent = lockedMsg;
  ["btn-draft", "btn-next", "btn-lock", "btn-flag"].forEach((id) => { $(id).disabled = locked; });
  $("btn-revise").disabled = !locked;
  $("revise-panel").hidden = !locked;
  $("reveal-panel").hidden = !locked;
  $("btn-flag").textContent = state.flagged ? "⚑ Flagged" : "⚑ Flag";

  // ma_role is forced NEITHER for non-M&A categories.
  const isMA = state.label.category === "M&A";
  document.querySelectorAll("#opts-ma_role .opt").forEach((el) => {
    const isNeither = el.dataset.value === "NEITHER";
    el.setAttribute("aria-disabled", (!isMA && !isNeither) ? "true" : "false");
  });

  const banner = $("banner");
  if (!state.sample.rubric_approved) {
    banner.hidden = false;
    banner.textContent = "Rubric not yet approved: locked labels are provisional and do NOT count toward canonical §11.4 progress. Approve the rubric in the Rubric panel.";
  } else { banner.hidden = true; }

  $("consensus-box").hidden = true;
  $("model-box").hidden = true;
  $("btn-reveal-consensus").disabled = !locked || state.sample.revealed.consensus;
  $("btn-reveal-model").disabled = !locked || state.sample.revealed.model;
  if (state.sample.consensus) renderConsensus(state.sample.consensus);
  if (state.sample.model) renderModel(state.sample.model);
}

function renderConsensus(c) {
  const box = $("consensus-box");
  box.hidden = false;
  box.innerHTML = "";
  const h = document.createElement("h3");
  h.textContent = `Prior consensus (${c.provenance})`;
  const p = document.createElement("p"); p.className = "muted"; p.textContent = c.disclaimer;
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(c.label, null, 2) + "\nnotes: " + (c.notes || "");
  box.append(h, p, pre);
}

function renderModel(m) {
  const box = $("model-box");
  box.hidden = false;
  box.innerHTML = "";
  const h = document.createElement("h3"); h.textContent = "Pinned model prediction";
  const pre = document.createElement("pre");
  if (m.available) {
    pre.textContent = `${m.model_pin}\n` + JSON.stringify(m.label, null, 2)
      + `\nconfidence: ${m.confidence}\nkeyword_override: ${m.keyword_override}`;
  } else {
    pre.textContent = `unavailable: ${m.reason}`;
  }
  box.append(h, pre);
}

// ---- interactions ----------------------------------------------------

function setField(field, value) {
  if (state.sample && state.sample.status === "LOCKED") return;
  if (field === "certainty") { state.certainty = value; }
  else {
    state.label[field] = value;
    if (field === "category" && value !== "M&A") state.label.ma_role = "NEITHER";
  }
  renderSample();
}

function currentLabel() {
  return {
    category: state.label.category || "",
    direction: state.label.direction || "",
    severity: state.label.severity || "",
    ma_role: state.label.ma_role || "",
  };
}

function baseBody() {
  return {
    dataset_id: state.datasetId,
    sample_id: state.sample.blind.sample_id,
    expected_version: state.version,
    notes: $("notes").value,
    certainty: state.certainty,
    label: currentLabel(),
  };
}

async function saveDraft() {
  // Returns true ONLY when the server confirmed persistence. Navigation and
  // other dependent flows must key off this, never assume success.
  try {
    const r = await api("/api/draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(baseBody()),
    });
    state.version = r.version;
    flash("Draft saved.");
    await refreshProgress();
    return true;
  } catch (e) {
    flash(`Draft NOT saved: ${e.message}`, true);
    return false;
  }
}

async function saveAndNext() {
  // Navigate ONLY after the server confirms the draft persisted. On failure
  // stay on the current headline with all input intact (the textarea and
  // option selections are untouched; state.version unchanged on error).
  const ok = await saveDraft();
  if (!ok) return;
  await next();
}

async function lock() {
  const l = currentLabel();
  if (!l.category || !l.direction || !l.severity || !l.ma_role) {
    flash("All four fields must be selected before locking.", true);
    return;
  }
  try {
    const r = await api("/api/lock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(baseBody()),
    });
    state.version = r.version;
    flash("Label locked.");
    await loadSampleById(state.sample.blind.sample_id);
    await refreshProgress();
  } catch (e) { flash(e.message, true); }
}

async function toggleFlag() {
  try {
    const r = await api("/api/flag", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dataset_id: state.datasetId,
        sample_id: state.sample.blind.sample_id,
        expected_version: state.version,
        flagged: !state.flagged,
      }),
    });
    state.version = r.version;
    state.flagged = !state.flagged;
    renderSample();
    await refreshProgress();
  } catch (e) { flash(e.message, true); }
}

async function reveal(kind) {
  try {
    const r = await api("/api/reveal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dataset_id: state.datasetId,
        sample_id: state.sample.blind.sample_id,
        expected_version: state.version,
        kind,
      }),
    });
    state.version = r.version;
    state.sample = r.sample;
    if (r.sample.consensus) renderConsensus(r.sample.consensus);
    if (r.sample.model) renderModel(r.sample.model);
    $("btn-reveal-consensus").disabled = true;
    $("btn-reveal-model").disabled = true;
  } catch (e) { flash(e.message, true); }
}

async function revise() {
  const reason = $("revise-reason").value.trim();
  if (!reason) { flash("A revision reason is required.", true); return; }
  try {
    const body = baseBody();
    body.reason = reason;
    const r = await api("/api/revise", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.version = r.version;
    $("revise-reason").value = "";
    flash("Revision recorded.");
    await loadSampleById(state.sample.blind.sample_id);
  } catch (e) { flash(e.message, true); }
}

async function approveRubric() {
  try {
    // Fetch the rubric live and approve its CURRENT content digest, so the
    // recorded approval is bound to exactly what the reviewer just read.
    const rub = await api("/api/rubric");
    const r = await api("/api/rubric/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rubric_version: rub.version,
        rubric_sha256: rub.sha256,
        note: $("rubric-note").value,
      }),
    });
    flash(`Rubric approved: ${r.approved}`);
    await refreshProgress();
  } catch (e) { flash(e.message, true); }
}

async function next() {
  if (state.idx + 1 < state._orderedIds.length) {
    await loadSampleAt(state.idx + 1);
  } else { flash("Last sample in this dataset."); }
}

async function prev() {
  if (state.idx > 0) await loadSampleAt(state.idx - 1);
}

async function refreshProgress() {
  const p = await api(`/api/progress?dataset=${encodeURIComponent(state.datasetId)}`);
  $("pg-ds").textContent = state.datasetId;
  $("pg-locked").textContent = p.locked;
  $("pg-canonical").textContent = p.canonical_locked;
  $("pg-draft").textContent = p.draft;
  $("pg-flagged").textContent = p.flagged;
  $("pg-remaining").textContent = p.remaining;
}

function flash(msg, isErr) {
  const el = $("statusline");
  el.textContent = msg;
  el.className = isErr ? "error small" : "muted small";
}

// ---- exports (GET streams bytes; no writes) --------------------------

function downloadExport(kind) {
  const url = `/api/export?dataset=${encodeURIComponent(state.datasetId)}&kind=${kind}`;
  window.location.assign(url);
}

// ---- wiring ----------------------------------------------------------

function wire() {
  $("pairbtn").addEventListener("click", doPair);
  $("paircode").addEventListener("keydown", (e) => { if (e.key === "Enter") doPair(); });
  $("btn-prev").addEventListener("click", prev);
  $("btn-next").addEventListener("click", saveAndNext);
  $("btn-draft").addEventListener("click", saveDraft);
  $("btn-lock").addEventListener("click", lock);
  $("btn-flag").addEventListener("click", toggleFlag);
  $("btn-revise").addEventListener("click", revise);
  $("btn-approve-rubric").addEventListener("click", approveRubric);
  $("btn-reveal-consensus").addEventListener("click", () => reveal("consensus"));
  $("btn-reveal-model").addEventListener("click", () => reveal("model"));
  $("btn-progress").addEventListener("click", async () => {
    await refreshProgress();
    $("progress-panel").hidden = !$("progress-panel").hidden;
  });
  $("btn-rubric").addEventListener("click", () => {
    $("rubric-panel").hidden = !$("rubric-panel").hidden;
  });
  document.querySelectorAll("[data-export]").forEach((b) => {
    b.addEventListener("click", () => downloadExport(b.dataset.export));
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (e.key === "ArrowRight") next();
    else if (e.key === "ArrowLeft") prev();
    else if (e.key === "s") saveDraft();
    else if (e.key === "f") toggleFlag();
    else if (e.key === "?") $("rubric-panel").hidden = !$("rubric-panel").hidden;
    // NOTE: no single-key lock binding, by design.
  });
}

wire();
boot();
