const $ = (selector) => document.querySelector(selector);

const state = {
  cases: [],
  selectedId: null,
  selected: null,
  runtime: null,
  definitions: {},
  activeEvidence: 0,
  activeStage: null,
  busy: false,
  toastTimer: null,
  expandedTimeline: new Set(),
};

const stages = [
  { id: "event", label: "Event", owner: "Source system", detail: "A synthetic checkout request exposes a real Inventory v2 contract failure." },
  { id: "investigate", label: "Investigate", owner: "2 child Workflows", detail: "Reproduction and change-intelligence agents gather independent evidence in parallel." },
  { id: "repair", label: "Repair", owner: "Ollama + Repair Agent", detail: "Ollama inspects the evidence, authors the replacement source, then scoped tools validate, test, and commit it on an isolated branch." },
  { id: "review", label: "Review", owner: "Review Agent", detail: "An independent agent inspects the exact diff and reruns the regression suite." },
  { id: "human", label: "Human", owner: "You + Temporal", detail: "Temporal waits durably for approval of the exact reviewed commit SHA." },
  { id: "merge", label: "Merge", owner: "Merge Activity", detail: "An idempotent Activity verifies the approved SHA and merges it to main." },
  { id: "verify", label: "Verify", owner: "Verification Agent", detail: "A separate agent tests main and replays the original checkout request." },
  { id: "outcome", label: "Outcome", owner: "SignalRoom", detail: "Measured business and technical outcomes close the case with an auditable record." },
];

const statusIndex = {
  queued: 0, preparing: 0, investigating: 1, repairing: 2, reviewing: 3,
  awaiting_merge_approval: 4, merging: 5, verifying: 6, completed: 7,
  rejected: 4, failed: 7,
};

const timelineKindLabels = {
  signalroom: "SignalRoom",
  temporal: "Temporal",
  worker: "Worker",
  event: "Event",
  model: "Model",
  tool: "Tool call",
  tool_result: "Tool result",
  handoff: "Handoff",
  human: "Human",
  git: "Git",
  outcome: "Outcome",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function pretty(value) {
  return typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 2);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed: ${response.status}`);
  return payload;
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { element.className = "toast"; }, 4200);
}

function short(value, length = 10) {
  if (!value) return "—";
  const text = String(value);
  return text.length > length ? text.slice(0, length) : text;
}

function clock(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function renderRuntime() {
  const runtime = state.runtime || {};
  const worker = runtime.worker || {};
  const ollama = runtime.ollama || {};
  const temporalOnline = Boolean(runtime.temporal_connected);
  const workerOnline = worker.status === "online";
  const workerRestarting = worker.status === "restarting" || worker.status === "starting";

  $("#temporal-status").textContent = temporalOnline ? "Connected" : "Unavailable";
  $("#temporal-status").className = temporalOnline ? "" : "offline";
  $("#worker-status").textContent = workerRestarting ? "Restarting…" : workerOnline ? "Online" : (worker.status || "Offline");
  $("#worker-status").className = workerRestarting ? "restarting" : workerOnline ? "" : "offline";
  $("#worker-detail").textContent = worker.worker_pid ? `PID ${worker.worker_pid} · executes tasks` : workerRestarting ? "Supervisor is replacing the process" : "No Worker is polling";
  $("#restart-count").textContent = `${worker.restart_count || 0} restart${worker.restart_count === 1 ? "" : "s"}`;
  $("#ollama-status").textContent = ollama.model_available ? short(ollama.model, 22) : "Ollama required";
  $("#ollama-status").className = ollama.model_available ? "" : "offline";
  $("#ollama-detail").textContent = ollama.model_available
    ? "Local reasoning · model-authored code"
    : ollama.connected ? `${ollama.model} is not pulled · repair disabled` : "Ollama offline · repair disabled";
  $("#runtime-indicator").textContent = temporalOnline && workerOnline ? "Temporal + Worker online" : workerRestarting ? "Worker recovery in progress" : "Runtime needs attention";
  $("#header-dot").className = `status-dot ${temporalOnline && workerOnline ? "online" : "offline"}`;

  const terminal = ["completed", "failed", "rejected"].includes(state.selected?.status);
  $("#kill-worker-button").disabled = !runtime.chaos_demo_enabled || !workerOnline || !state.selected || terminal || state.busy;
  $("#kill-worker-button").classList.toggle("armed", Boolean(state.selected && workerOnline && !terminal));
}

function renderPipeline() {
  const current = state.selected ? (statusIndex[state.selected.status] ?? 0) : -1;
  $("#pipeline").innerHTML = stages.map((stage, index) => {
    let phase = "waiting";
    if (state.selected) {
      if (state.selected.status === "completed") phase = "complete";
      else if (index < current) phase = "complete";
      else if (index === current) phase = ["failed", "rejected"].includes(state.selected.status) ? "blocked" : "active";
    }
    const marker = phase === "complete" ? "✓" : String(index + 1).padStart(2, "0");
    return `<button class="stage ${phase}" data-stage="${index}" aria-label="${escapeHtml(stage.label)} stage, ${phase}">
      <span class="stage-node">${marker}</span><strong>${escapeHtml(stage.label)}</strong><small>${escapeHtml(stage.owner)}</small>
    </button>`;
  }).join("");
  const selectedStage = state.activeStage == null ? (current >= 0 ? current : null) : state.activeStage;
  inspectStage(selectedStage, false);
}

function inspectStage(index, persist = true) {
  if (index == null || !stages[index]) {
    $("#stage-inspector").innerHTML = "<span>Stage</span><div><strong>Select a point in the map</strong><p>Its owner, evidence, and durable boundary will appear here.</p></div><b>Waiting</b>";
    return;
  }
  if (persist) state.activeStage = index;
  const stage = stages[index];
  const current = state.selected ? (statusIndex[state.selected.status] ?? 0) : -1;
  const label = !state.selected ? "Waiting" : index < current || state.selected.status === "completed" ? "Recorded" : index === current ? state.selected.status.replaceAll("_", " ") : "Pending";
  $("#stage-inspector").innerHTML = `<span>${String(index + 1).padStart(2, "0")}</span>
    <div><strong>${escapeHtml(stage.label)} · ${escapeHtml(stage.owner)}</strong><p>${escapeHtml(stage.detail)}</p></div><b>${escapeHtml(label)}</b>`;
}

function renderCases() {
  $("#case-count").textContent = state.cases.length;
  if (!state.cases.length) {
    $("#case-list").innerHTML = '<p class="empty">No cases yet. Run the pipeline above.</p>';
    return;
  }
  $("#case-list").innerHTML = state.cases.map(item => `<button class="case-item ${item.id === state.selectedId ? "selected" : ""}" data-case="${escapeHtml(item.id)}">
    <span><b>${escapeHtml(item.id)}</b><time>${escapeHtml(clock(item.updated_at))}</time></span>
    <strong>${escapeHtml(item.title)}</strong>
    <i class="status-chip ${escapeHtml(item.status)}">${escapeHtml(item.status.replaceAll("_", " "))}</i>
  </button>`).join("");
}

function renderCase() {
  const item = state.selected;
  if (!item) {
    $("#case-meta").textContent = "Trigger a case to see each durable transition.";
    $("#case-content").className = "case-content empty-state";
    $("#case-content").innerHTML = '<span class="empty-icon">↯</span><h2>No active case</h2><p>Trigger the synthetic contract failure. The repository, branch, commits, tests, and merge are real and isolated locally.</p>';
    return;
  }
  $("#case-meta").textContent = `${item.id} · ${item.event?.source || "local source"} · updated ${clock(item.updated_at)}`;
  const outcome = item.outcome;
  const commit = item.commit?.commit || item.evidence?.commit?.commit;
  $("#case-content").className = "case-content";
  $("#case-content").innerHTML = `
    <div class="case-kicker"><span class="status-chip ${escapeHtml(item.status)}">${escapeHtml(item.status.replaceAll("_", " "))}</span><span class="eyebrow">${escapeHtml(item.workflow_id || "")}</span></div>
    <h2>${escapeHtml(item.title)}</h2>
    <p class="case-summary">${escapeHtml(item.event?.summary || "")}</p>
    <div class="case-facts">
      <div class="fact"><span>Repository</span><strong>${escapeHtml(item.lab?.repo || "Preparing…")}</strong></div>
      <div class="fact"><span>Repair branch</span><strong>${escapeHtml(item.branch || item.evidence?.commit?.branch || "Not created")}</strong></div>
      <div class="fact"><span>Reviewed commit</span><strong title="${escapeHtml(commit || "")}">${escapeHtml(short(commit, 12))}</strong></div>
      <div class="fact"><span>Temporal state</span><strong>${escapeHtml(item.status.replaceAll("_", " "))}</strong></div>
    </div>
    ${outcome ? `<div class="outcome-card"><div><strong>${outcome.success ? "Outcome reached" : "Outcome failed"}</strong><p>${outcome.success ? "Checkout accepts Inventory v2 and the regression suite passes on main." : "Verification did not satisfy the outcome contract."}</p></div><span class="status-chip ${outcome.success ? "completed" : "failed"}">${outcome.success ? "verified" : "failed"}</span></div>` : ""}
  `;
}

function renderAgents() {
  const agentRecords = state.selected?.agents || {};
  const definitions = state.definitions || {};
  const entries = Object.entries(definitions);
  if (!entries.length) {
    $("#agent-grid").innerHTML = '<p class="empty">Loading agent definitions…</p>';
    return;
  }
  $("#agent-grid").innerHTML = entries.map(([id, definition]) => {
    const record = agentRecords[id] || { status: "queued", turn: 0, observations: [] };
    const used = new Set((record.observations || []).map(item => item.tool));
    const current = record.active_tool
      ? `Calling ${record.active_tool}`
      : record.last_summary || (record.status === "completed" ? record.result?.summary : "Waiting for its durable assignment.");
    return `<article class="agent-card" data-status="${escapeHtml(record.status || "queued")}" data-accent="${escapeHtml(definition.accent || "acid")}">
      <div class="agent-top"><span class="agent-status"><i></i>${escapeHtml((record.status || "queued").replaceAll("_", " "))}</span><span class="agent-turn">turn ${record.turn || 0}/${definition.max_turns}</span></div>
      <h3>${escapeHtml(definition.name)}</h3>
      <p class="agent-purpose">${escapeHtml(definition.purpose)}</p>
      <div class="agent-current">${escapeHtml(current || "Assignment complete.")}</div>
      <div class="agent-meta"><span>${escapeHtml(record.model_mode || "not started")}</span><span>${(record.observations || []).length} results</span></div>
      <div class="agent-tools">${definition.tools.map(tool => `<span class="tool-pill ${used.has(tool) ? "used" : ""}">${escapeHtml(tool)}</span>`).join("")}</div>
    </article>`;
  }).join("");
}

function evidenceItems() {
  if (!state.selected) return [];
  const definitions = state.definitions;
  const items = [];
  Object.entries(state.selected.agents || {}).forEach(([agentId, agent]) => {
    (agent.observations || []).forEach(observation => items.push({
      title: observation.tool,
      source: definitions[agentId]?.name || agentId,
      value: observation.result,
    }));
  });
  if (state.selected.merge) items.push({ title: "git.merge", source: "Merge Activity", value: state.selected.merge });
  if (state.selected.outcome) items.push({ title: "verified.outcome", source: "Outcome Verification", value: state.selected.outcome });
  return items;
}

function renderEvidence() {
  const items = evidenceItems();
  if (!items.length) {
    $("#evidence-tabs").innerHTML = "";
    $("#evidence-viewer").innerHTML = '<p class="empty">Agent tool results will appear here as the Workflow progresses.</p>';
    return;
  }
  state.activeEvidence = Math.min(state.activeEvidence, items.length - 1);
  $("#evidence-tabs").innerHTML = items.map((item, index) => `<button class="evidence-tab ${index === state.activeEvidence ? "active" : ""}" data-evidence="${index}">${escapeHtml(item.title)}</button>`).join("");
  const item = items[state.activeEvidence];
  $("#evidence-viewer").innerHTML = `<h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.source)} · recorded tool evidence</p><pre><code>${escapeHtml(pretty(item.value))}</code></pre>`;
}

function renderTimeline() {
  document.querySelectorAll("#timeline details[data-timeline-id][open]").forEach(details => {
    state.expandedTimeline.add(details.dataset.timelineId);
  });
  const timeline = state.selected?.timeline || [];
  const visibleKinds = [...new Set(timeline.map(item => item.kind || "unknown"))];
  $("#timeline-legend").innerHTML = Object.keys(timelineKindLabels)
    .filter(kind => visibleKinds.includes(kind))
    .map(kind => `<span data-kind="${escapeHtml(kind)}">${escapeHtml(timelineKindLabels[kind])}</span>`)
    .join("");
  if (!timeline.length) {
    $("#timeline").innerHTML = '<li class="empty">No transitions recorded.</li>';
    return;
  }
  $("#timeline").innerHTML = [...timeline].reverse().map(item => `<li class="${escapeHtml(item.status || "")}" data-kind="${escapeHtml(item.kind || "unknown")}">
    <time>${escapeHtml(clock(item.at))} · ${escapeHtml(item.kind)}</time>
    <strong>${escapeHtml(item.title)}</strong>
    <p>${escapeHtml(item.detail || "")}</p>
    ${item.data != null ? `<details data-timeline-id="${escapeHtml(item.id)}" ${state.expandedTimeline.has(item.id) ? "open" : ""}><summary>Inspect recorded payload</summary><pre><code>${escapeHtml(pretty(item.data))}</code></pre></details>` : ""}
  </li>`).join("");
}

function renderApproval() {
  const item = state.selected;
  const visible = item?.status === "awaiting_merge_approval";
  $("#approval-panel").hidden = !visible;
  if (!visible) return;
  const evidence = item.evidence || {};
  const commit = evidence.commit || {};
  const review = evidence.review || {};
  const tests = evidence.tests || {};
  $("#approval-facts").innerHTML = `
    <div><dt>Branch</dt><dd>${escapeHtml(commit.branch || "—")}</dd></div>
    <div><dt>Commit</dt><dd>${escapeHtml(commit.commit || "—")}</dd></div>
    <div><dt>Tests</dt><dd>${tests.exit_code === 0 ? "Passing · exit 0" : `Exit ${escapeHtml(tests.exit_code)}`}</dd></div>
    <div><dt>Review</dt><dd>${review.approved ? "Approved · no blocking concerns" : "Not approved"}</dd></div>`;
  $("#diff-code").textContent = review.diff || "No diff evidence recorded.";
}

function renderAll() {
  renderRuntime();
  renderCases();
  renderCase();
  renderPipeline();
  renderAgents();
  renderEvidence();
  renderTimeline();
  renderApproval();
}

async function refresh() {
  try {
    const [caseResponse, runtime, definitionResponse] = await Promise.all([
      api("/api/cases"), api("/api/runtime"), api("/api/agent-definitions"),
    ]);
    state.cases = caseResponse.cases || [];
    state.runtime = runtime;
    state.definitions = definitionResponse.agents || {};
    if (!state.selectedId && state.cases.length) state.selectedId = state.cases[0].id;
    const summary = state.cases.find(item => item.id === state.selectedId);
    if (summary) state.selected = await api(`/api/cases/${encodeURIComponent(summary.id)}`);
    else state.selected = null;
    renderAll();
  } catch (error) {
    $("#runtime-indicator").textContent = "SignalRoom API unavailable";
    $("#header-dot").className = "status-dot offline";
  }
}

async function triggerCase() {
  if (state.busy) return;
  state.busy = true;
  document.querySelectorAll("#trigger-button,#hero-trigger").forEach(button => { button.disabled = true; button.textContent = "Starting durable case…"; });
  try {
    const response = await api("/api/lab/run", { method: "POST", body: JSON.stringify({ step_delay: 0.42 }) });
    state.selectedId = response.case.id;
    state.activeEvidence = 0;
    state.activeStage = null;
    state.expandedTimeline.clear();
    toast("Durable case started. Two investigation agents are being scheduled.");
    await refresh();
    document.querySelector(".map-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.busy = false;
    document.querySelectorAll("#trigger-button,#hero-trigger").forEach((button, index) => { button.disabled = false; button.innerHTML = index === 0 ? 'Trigger incident <span>↗</span>' : 'Run the repair pipeline <span>↗</span>'; });
  }
}

async function killWorker() {
  if (!state.selected || state.busy) return;
  state.busy = true;
  renderRuntime();
  try {
    const response = await api("/api/demo/worker/crash", { method: "POST", body: JSON.stringify({ case_id: state.selected.id }) });
    toast(`Worker ${response.killed_worker_pid} killed. Watch the supervisor and Temporal recovery.`);
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.busy = false;
  }
}

async function mergeDecision(approved) {
  if (!state.selected || state.busy) return;
  state.busy = true;
  $("#approve-button").disabled = true;
  $("#reject-button").disabled = true;
  try {
    await api(`/api/cases/${encodeURIComponent(state.selected.id)}/${approved ? "approve-merge" : "reject-merge"}`, {
      method: "POST", body: JSON.stringify({ note: approved ? "Operator reviewed diff, tests, and exact commit." : "Operator rejected the proposed repair." }),
    });
    toast(approved ? "Approval signal accepted. Temporal is resuming the Workflow." : "Rejection recorded. Temporal is closing the case.");
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.busy = false;
    $("#approve-button").disabled = false;
    $("#reject-button").disabled = false;
  }
}

document.addEventListener("click", async event => {
  const caseButton = event.target.closest("[data-case]");
  if (caseButton) {
    state.selectedId = caseButton.dataset.case;
    state.activeEvidence = 0;
    state.activeStage = null;
    state.expandedTimeline.clear();
    await refresh();
    return;
  }
  const stageButton = event.target.closest("[data-stage]");
  if (stageButton) { inspectStage(Number(stageButton.dataset.stage), true); return; }
  const evidenceButton = event.target.closest("[data-evidence]");
  if (evidenceButton) { state.activeEvidence = Number(evidenceButton.dataset.evidence); renderEvidence(); return; }
  const copyButton = event.target.closest("[data-copy]");
  if (copyButton) {
    const source = document.getElementById(copyButton.dataset.copy);
    await navigator.clipboard.writeText(source?.textContent || "");
    toast("Copied to clipboard.");
  }
});

$("#timeline").addEventListener("toggle", event => {
  const details = event.target.closest?.("details[data-timeline-id]");
  if (!details) return;
  if (details.open) state.expandedTimeline.add(details.dataset.timelineId);
  else state.expandedTimeline.delete(details.dataset.timelineId);
}, true);

$("#trigger-button").addEventListener("click", triggerCase);
$("#hero-trigger").addEventListener("click", triggerCase);
$("#kill-worker-button").addEventListener("click", killWorker);
$("#approve-button").addEventListener("click", () => mergeDecision(true));
$("#reject-button").addEventListener("click", () => mergeDecision(false));
$("#definitions-button").addEventListener("click", () => {
  $("#definitions-code").textContent = JSON.stringify(state.definitions, null, 2);
  $("#definitions-dialog").showModal();
});
$("#close-definitions").addEventListener("click", () => $("#definitions-dialog").close());
$("#definitions-dialog").addEventListener("click", event => {
  if (event.target === $("#definitions-dialog")) $("#definitions-dialog").close();
});

refresh();
setInterval(refresh, 900);
