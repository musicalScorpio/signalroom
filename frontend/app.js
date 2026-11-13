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
  activeRecoveryId: null,
  recoveryContextOpen: false,
  caseContextOpen: false,
  timelineFilter: "recovery",
  provider: null,
  providerRefreshedAt: null,
  refreshInFlight: false,
  expandedExternalTimeline: new Set(),
  scrollPositions: new Map(),
};

const reliabilityStrategies = {
  none: {
    label: "Code repair only · no external API",
    detail: "The agents repair, review, merge, and verify the contract defect without an external side effect.",
  },
  native_idempotency: {
    label: "Idempotent API · retry POST with the same key",
    detail: "The repaired checkout retries POST with one stable Idempotency-Key. The provider returns the first receipt and creates nothing new.",
  },
  at_most_once: {
    label: "Non-idempotent API · maximumAttempts=1 (0 retries)",
    detail: "The repaired checkout permits one POST attempt and zero retries. The replacement agent stops rather than risk a duplicate.",
  },
  validation: {
    label: "Non-idempotent API · 0 POST retries + GET recovery",
    detail: "The repaired checkout never retries the unsafe POST. The recovery agent uses only safe GET checks after 1, 2, and 4 second backoffs.",
  },
};

const stages = [
  { id: "event", label: "Event", owner: "Source system", detail: "A synthetic checkout request exposes a real Inventory v2 contract failure." },
  { id: "investigate", label: "Investigate", owner: "2 child Workflows", detail: "Reproduction and change-intelligence agents gather independent evidence in parallel." },
  { id: "repair", label: "Repair", owner: "Ollama + Repair Agent", detail: "Ollama inspects the evidence, authors the replacement source, then scoped tools validate, test, and commit it on an isolated branch." },
  { id: "review", label: "Review", owner: "Review Agent", detail: "An independent agent inspects the exact diff and reruns the regression suite." },
  { id: "external", label: "External proof", owner: "Side-Effect Agent + Temporal", detail: "The reviewed branch calls the local provider under the selected retry or reconciliation policy." },
  { id: "human", label: "Human", owner: "You + Temporal", detail: "Temporal waits durably for approval of the exact reviewed commit and provider evidence." },
  { id: "merge", label: "Merge", owner: "Merge Activity", detail: "An idempotent Activity verifies the approved SHA and merges it to main." },
  { id: "verify", label: "Verify", owner: "Verification Agent", detail: "A separate agent tests main and replays the original checkout request without repeating the live side effect." },
  { id: "outcome", label: "Outcome", owner: "SignalRoom", detail: "Measured business and technical outcomes close the case with an auditable record." },
];

const statusIndex = {
  queued: 0, preparing: 0, investigating: 1, repairing: 2, reviewing: 3,
  proving_external_effect: 4, calling_external_api: 4, reconciling_external_effect: 4,
  external_effect_proven: 4, external_effect_skipped: 4,
  awaiting_merge_approval: 5, merging: 6, verifying: 7, completed: 8,
  rejected: 5, failed: 8,
};

const timelineKindLabels = {
  signalroom: "SignalRoom",
  temporal: "Temporal",
  worker: "Worker",
  event: "Event",
  model: "Model",
  agent: "Agent",
  tool: "Tool call",
  tool_result: "Tool result",
  handoff: "Handoff",
  human: "Human",
  git: "Git",
  outcome: "Outcome",
  provider: "Provider",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function pretty(value) {
  return typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 2);
}

function captureScrollState() {
  document.querySelectorAll("[data-scroll-key]").forEach(element => {
    state.scrollPositions.set(element.dataset.scrollKey, { top: element.scrollTop, left: element.scrollLeft });
  });
  return { x: window.scrollX, y: window.scrollY };
}

function restoreScrollState(viewport) {
  document.querySelectorAll("[data-scroll-key]").forEach(element => {
    const position = state.scrollPositions.get(element.dataset.scrollKey);
    if (!position) return;
    element.scrollTop = position.top;
    element.scrollLeft = position.left;
  });
  window.scrollTo(viewport.x, viewport.y);
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
  $("#ollama-status").textContent = ollama.model_available ? short(ollama.model, 22) : "No local model";
  $("#ollama-status").className = ollama.model_available ? "" : "offline";
  $("#ollama-detail").textContent = ollama.model_available
    ? `Local reasoning · ${ollama.selection_source || "selected"} model`
    : ollama.connected ? "No installed model found · repair disabled" : "Ollama offline · repair disabled";
  $("#runtime-indicator").textContent = temporalOnline && workerOnline ? "Temporal + Worker online" : workerRestarting ? "Worker recovery in progress" : "Runtime needs attention";
  $("#header-dot").className = `status-dot ${temporalOnline && workerOnline ? "online" : "offline"}`;

  const terminal = ["completed", "failed", "rejected"].includes(state.selected?.status);
  const reliabilityDanger = Boolean(state.selected?.side_effect?.danger_window);
  const caseActive = Boolean(state.selected && !terminal);
  const killAvailable = runtime.chaos_demo_enabled && workerOnline && caseActive && !state.busy;
  $("#kill-worker-button").disabled = !killAvailable;
  $("#kill-worker-button").classList.toggle("armed", killAvailable);
  $("#kill-worker-label").textContent = reliabilityDanger ? "Kill Worker — result gap" : "Kill Agent Worker";
}

function renderReliability() {
  const run = state.selected;
  const effect = run?.side_effect || {};
  const strategy = reliabilityStrategies[effect.strategy || "none"];
  $("#strategy-note").innerHTML = run
    ? `<strong>${escapeHtml(strategy.label)}</strong> · ${escapeHtml(strategy.detail)}`
    : "Select a strategy above and run the repair pipeline. API proof stays attached to the same case, branch, and reviewed commit.";
  const providerOnline = state.runtime?.third_party?.status === "ok";
  $("#provider-health").textContent = providerOnline ? "Local service online" : "Service offline";
  $("#provider-health").className = `provider-health${providerOnline ? "" : " offline"}`;
  $("#provider-refresh").textContent = state.providerRefreshedAt
    ? `Live · refreshed ${clock(state.providerRefreshedAt)}` : "Refreshing…";
  $("#reliability-run-status").textContent = run ? (effect.status || "waiting").replaceAll("_", " ") : "idle";
  $("#reliability-run-status").className = `status-chip ${escapeHtml(effect.status || "")}`;

  if (!run) {
    $("#reliability-status").innerHTML = '<p class="empty">Run a case to see its code-to-provider proof.</p>';
    $("#reliability-timeline").innerHTML = '<li class="empty">No API events in this case.</li>';
    $("#external-call-flow").innerHTML = "";
  } else {
    const resolution = effect.resolution || (effect.danger_window ? "provider committed · result not durable" : "waiting for reviewed code");
    $("#reliability-status").innerHTML = `<div class="reliability-facts">
      <div><span>Strategy</span><strong>${escapeHtml(strategy.label)}</strong></div>
      <div><span>Workflow status</span><strong>${escapeHtml(run.status.replaceAll("_", " "))}</strong></div>
      <div><span>Activity attempt</span><strong>${escapeHtml(effect.activity_attempt || 0)}</strong></div>
      <div><span>Auto crash</span><strong>${effect.auto_crash ? escapeHtml(effect.auto_crash_state || "armed") : "off"}</strong></div>
      <div><span>Payment code</span><strong>${escapeHtml(effect.payment_code || "not applicable")}</strong></div>
      <div><span>Resolution</span><strong>${escapeHtml(String(resolution).replaceAll("_", " "))}</strong></div>
    </div>`;
    document.querySelectorAll("#reliability-timeline details[data-reliability-timeline-id][open]").forEach(details => {
      state.expandedExternalTimeline.add(details.dataset.reliabilityTimelineId);
    });
    const timeline = (run.timeline || []).filter(item => ["provider", "agent", "worker", "temporal"].includes(item.kind)
      && /external|provider|side-effect|worker|replay/i.test(`${item.title} ${item.detail || ""}`));
    $("#reliability-timeline").innerHTML = timeline.length ? [...timeline].reverse().map(item => `<li data-kind="${escapeHtml(item.kind || "signalroom")}">
      <time>${escapeHtml(clock(item.at))} · ${escapeHtml(item.kind)}</time>
      <strong>${escapeHtml(item.title)}</strong>
      <p>${escapeHtml(item.detail || "")}</p>
      ${item.data != null ? `<details data-reliability-timeline-id="${escapeHtml(item.id)}" ${state.expandedExternalTimeline.has(item.id) ? "open" : ""}><summary>Inspect full recorded message</summary><pre data-scroll-key="external:${escapeHtml(run.id)}:${escapeHtml(item.id)}"><code>${escapeHtml(pretty(item.data))}</code></pre></details>` : ""}
    </li>`).join("") : '<li class="empty">Waiting for the reviewed branch to reach the provider proof.</li>';

    const flowStep = effect.status === "provider_committed" ? 3
      : ["provider_replayed", "completed"].includes(effect.status) || effect.resolution ? 6
      : run.status === "reconciling_external_effect" ? 5
      : effect.activity_attempt ? 2
      : ["proving_external_effect", "calling_external_api"].includes(run.status) ? 1 : 0;
    const nodes = [
      ["Reviewed code", "model-authored checkout"], ["POST", strategy.label],
      ["Provider DB", "separate local truth"], ["Result gap", effect.auto_crash ? "Worker auto-kill" : "manual / no crash"],
      ["Temporal replay", "same operation + policy"],
      [effect.strategy === "validation" ? "GET reconcile" : effect.strategy === "at_most_once" ? "Stop retry" : "Safe retry", "no duplicate"],
      ["Evidence", "human merge gate"],
    ];
    $("#external-call-flow").innerHTML = effect.strategy === "none"
      ? '<div class="external-flow-empty">No provider call selected for this case.</div>'
      : nodes.map(([label, note], index) => `<article class="external-flow-node ${index < flowStep ? "complete" : index === flowStep ? "active" : "waiting"}"><i></i><strong>${escapeHtml(label)}</strong><span>${escapeHtml(note)}</span></article>`).join('<b class="external-flow-link">→</b>');
  }

  const provider = state.provider || { payments: [], audit: [] };
  const payments = provider.payments || [];
  const audit = provider.audit || [];
  if ((!run || effect.strategy === "none") && !payments.length && !audit.length) {
    $("#provider-data").innerHTML = '<p class="empty">Provider records will appear here.</p>';
    return;
  }
  $("#provider-data").innerHTML = `
    ${effect.payment_code ? `<div class="provider-scope"><span>Showing evidence for this case only</span><strong>${escapeHtml(effect.payment_code)} · $${(Number(effect.amount_cents || 0) / 100).toFixed(2)}</strong></div>` : ""}
    <div class="provider-summary">
      <div><span>Payments created</span><strong>${payments.length}</strong></div>
      <div><span>POST requests</span><strong>${audit.filter(item => item.method === "POST").length}</strong></div>
      <div><span>GET checks</span><strong>${audit.filter(item => item.method === "GET").length}</strong></div>
    </div>
    <h4>Payment rows</h4>
    ${payments.length ? `<table class="provider-table"><thead><tr><th>ID</th><th>Code</th><th>Route</th><th>Amount</th></tr></thead><tbody>${payments.map(payment => `<tr><td><code>${escapeHtml(payment.id)}</code></td><td>${escapeHtml(payment.payment_code)}</td><td>${escapeHtml(payment.route)}</td><td>$${(Number(payment.amount_cents) / 100).toFixed(2)}</td></tr>`).join("")}</tbody></table>` : '<p class="empty">No payment row yet.</p>'}
    <h4>Provider request audit</h4>
    ${audit.length ? `<table class="provider-table"><thead><tr><th>Method</th><th>Route</th><th>Outcome</th><th>Payment</th></tr></thead><tbody>${audit.map(item => `<tr><td>${escapeHtml(item.method)}</td><td>${escapeHtml(item.route)}</td><td>${escapeHtml(item.outcome)}</td><td><code>${escapeHtml(item.payment_id || "—")}</code></td></tr>`).join("")}</tbody></table>` : '<p class="empty">No provider requests yet.</p>'}`;
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

function recoveryCycles(record = state.selected) {
  const cycles = [];
  const byKilledPid = new Map();
  let current = null;
  for (const event of record?.timeline || []) {
    if (["Agent Worker killed", "Agent Worker killed automatically in the result gap"].includes(event.title)) {
      const cycle = event.data?.recovery_cycle || cycles.length + 1;
      current = { id: event.data?.recovery_id || event.id, cycle, killed: event, retained: null, replacement: null, replay: null, reattached: null, resumed: null };
      cycles.push(current);
      if (event.data?.worker_pid) byKilledPid.set(String(event.data.worker_pid), current);
      continue;
    }
    if (!current) continue;
    if (["Temporal retained the durable checkpoint", "Temporal retained the unresolved external-effect Activity"].includes(event.title)) current.retained = event;
    else if (["Replacement Worker connected", "Replacement Worker resumed the external-effect Activity"].includes(event.title)) {
      const matched = byKilledPid.get(String(event.data?.killed_worker_pid)) || current;
      matched.replacement = event;
    } else if (["Temporal replay restored Workflow context", "Temporal replay query pending", "Temporal replayed the unresolved side-effect assignment"].includes(event.title)) current.replay = event;
    else if (event.title === "SignalRoom reattached to the recovered case") current.reattached = event;
    else if (event.kind === "model" && (event.detail || "").includes("Replacement Worker resumed")) {
      const target = [...cycles].reverse().find(item => item.replacement && !item.resumed);
      if (target) target.resumed = event;
    }
  }
  return cycles;
}

function selectedRecoveryCycle(record = state.selected) {
  const cycles = recoveryCycles(record);
  if (!cycles.length) return null;
  const selected = cycles.find(item => item.id === state.activeRecoveryId) || cycles[cycles.length - 1];
  state.activeRecoveryId = selected.id;
  return selected;
}

function renderRecoveryProof() {
  const host = $("#recovery-proof");
  const cycles = recoveryCycles();
  if (!cycles.length) {
    host.hidden = true;
    host.innerHTML = "";
    return;
  }
  host.hidden = false;
  const selected = selectedRecoveryCycle();
  const killData = selected.killed?.data || {};
  const replacementData = selected.replacement?.data || {};
  const replayData = selected.replay?.data || {};
  const interrupted = killData.interrupted_agents || [];
  const primary = interrupted[0] || {};
  const liveAgent = state.selected?.agents?.[primary.agent_id] || {};
  const recoveredState = replayData.recovered_agent_states?.[primary.agent_id] || {};
  const exactAgentQuery = Boolean(recoveredState.assignment_context);
  const assignmentContext = recoveredState.assignment_context || replayData.replayed_context || replayData.signalroom_projection || selected.retained?.data?.context_to_reconstruct || {};
  const observations = recoveredState.observations || liveAgent.observations || [];
  const completedAgents = replayData.replayed_context?.completed_agents || replayData.recovered_workflow_state?.context_checkpoint?.completed_agents || assignmentContext.completed_agents || [];
  const observationTools = observations.map(item => item.tool).filter(Boolean);
  const replayConfirmed = ["Temporal replay restored Workflow context", "Temporal replayed the unresolved side-effect assignment"].includes(selected.replay?.title);
  const checkpoint = replayData.recovered_workflow_state?.status || replayData.signalroom_projection?.checkpoint_status || selected.retained?.data?.context_to_reconstruct?.checkpoint_status || killData.checkpoint_status || state.selected?.status || "unknown";
  const agentName = primary.name || recoveredState.agent_name || primary.agent_id || "Active agent";
  const pickedUp = [
    completedAgents.length ? `${completedAgents.length} completed agent handoff${completedAgents.length === 1 ? "" : "s"}` : null,
    observationTools.length ? `${observationTools.length} prior tool result${observationTools.length === 1 ? "" : "s"}` : null,
    assignmentContext.original_failure || assignmentContext.failure ? "original failure" : null,
    assignmentContext.repository || assignmentContext.repo ? "repository assignment" : null,
  ].filter(Boolean);
  const nextAction = selected.resumed?.detail || liveAgent.last_summary || "Waiting for Temporal to schedule the unfinished durable step.";
  host.innerHTML = `
    <div class="recovery-proof-head">
      <div><span>Durability proof</span><strong>${replayConfirmed ? "Replay confirmed" : "Checkpoint retained"}</strong></div>
      <div class="recovery-cycle-picker">${cycles.map(item => `<button class="${item.id === selected.id ? "active" : ""}" data-recovery-id="${escapeHtml(item.id)}">Recovery ${escapeHtml(item.cycle)}</button>`).join("")}</div>
    </div>
    <div class="recovery-flow ${replayConfirmed ? "recovered" : "retained"}">
      <article class="recovery-node crash">
        <span>01 · process lost</span><strong>${escapeHtml(agentName)}</strong>
        <p>Turn ${escapeHtml(primary.turn || recoveredState.turn || "?")} interrupted on Worker ${escapeHtml(killData.worker_pid || "unknown")}.</p>
      </article>
      <div class="recovery-link broken" aria-label="Worker connection broken"><i></i><b>process died</b></div>
      <article class="recovery-node temporal">
        <span>02 · Temporal held</span><strong>${escapeHtml(checkpoint)}</strong>
        <p>Workflow history kept completed results and the unfinished assignment.</p>
      </article>
      <div class="recovery-link restored" aria-label="Context replayed to replacement Worker"><i></i><b>history replay</b></div>
      <article class="recovery-node resumed">
        <span>03 · agent resumed</span><strong>${escapeHtml(agentName)}</strong>
        <p>Worker ${escapeHtml(replacementData.replacement_worker_pid || "pending")} picked up turn ${escapeHtml(recoveredState.turn || primary.turn || "?")}.</p>
      </article>
    </div>
    <div class="agent-context-handoff">
      <div class="context-title"><span>${exactAgentQuery ? "Temporal child-Workflow query" : "Recorded recovery projection"}</span><strong>${escapeHtml(agentName)} did not start from scratch</strong></div>
      <dl>
        <div><dt>Durable identity</dt><dd>${escapeHtml(state.selected?.workflow_id || "—")}</dd></div>
        <div><dt>Restored context</dt><dd>${pickedUp.length ? pickedUp.map(value => `<span>${escapeHtml(value)}</span>`).join("") : "Checkpoint available; child context query pending"}</dd></div>
        <div><dt>Prior tools</dt><dd>${observationTools.length ? observationTools.map(value => `<span>${escapeHtml(value)}</span>`).join("") : "No completed tool calls before this crash"}</dd></div>
        <div><dt>Next action</dt><dd>${escapeHtml(nextAction)}</dd></div>
      </dl>
      <details data-recovery-context ${state.recoveryContextOpen ? "open" : ""}><summary>${exactAgentQuery ? "Inspect exact agent context from Temporal" : "Inspect recorded recovery context"}</summary><pre data-scroll-key="recovery:${escapeHtml(state.selected?.id)}:${escapeHtml(selected.id)}"><code>${escapeHtml(pretty({ checkpoint, interrupted_agent: primary, recovered_agent_state: recoveredState, assignment_context: assignmentContext, recovered_observations: observations }))}</code></pre></details>
    </div>`;
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
    <details class="case-context" data-case-context ${state.caseContextOpen ? "open" : ""}>
      <summary>Inspect durable agent context</summary>
      <pre data-scroll-key="case-context:${escapeHtml(item.id)}"><code>${escapeHtml(pretty({
        event: item.event,
        context: item.context,
        side_effect: item.side_effect,
        reviewed_commit: commit,
        approval: item.approval,
      }))}</code></pre>
    </details>
    ${outcome ? `<div class="outcome-card"><div><strong>${outcome.success ? "Outcome reached" : "Outcome failed"}</strong><p>${outcome.success ? "Checkout accepts Inventory v2 and the regression suite passes on main." : "Verification did not satisfy the outcome contract."}</p></div><span class="status-chip ${outcome.success ? "completed" : "failed"}">${outcome.success ? "verified" : "failed"}</span></div>` : ""}
  `;
}

function renderAgents() {
  const agentRecords = state.selected?.agents || {};
  const definitions = state.definitions || {};
  const entries = Object.entries(definitions);
  const recovery = selectedRecoveryCycle();
  const recoveryAgents = new Map((recovery?.killed?.data?.interrupted_agents || []).map(agent => [agent.agent_id, agent]));
  if (!entries.length) {
    $("#agent-grid").innerHTML = '<p class="empty">Loading agent definitions…</p>';
    return;
  }
  $("#agent-grid").innerHTML = entries.map(([id, definition]) => {
    const record = agentRecords[id] || { status: "queued", turn: 0, observations: [] };
    const used = new Set((record.observations || []).map(item => item.tool));
    const current = record.last_summary
      || (record.active_tool ? `Running ${record.active_tool} locally.` : null)
      || (record.status === "completed"
        ? record.result?.summary
        : "Not scheduled yet; waiting for the previous pipeline stage to finish.");
    const recoveryAgent = recoveryAgents.get(id);
    return `<article class="agent-card ${recoveryAgent ? "recovery-focus" : ""}" data-agent-id="${escapeHtml(id)}" data-status="${escapeHtml(record.status || "queued")}" data-accent="${escapeHtml(definition.accent || "acid")}">
      <div class="agent-top"><span class="agent-status"><i></i>${escapeHtml((record.status || "queued").replaceAll("_", " "))}</span><span class="agent-turn">turn ${record.turn || 0}/${definition.max_turns}</span></div>
      <h3>${escapeHtml(definition.name)}</h3>
      ${recoveryAgent ? `<div class="agent-recovery-tag">Recovery ${escapeHtml(recovery.cycle)} · resumed from turn ${escapeHtml(recoveryAgent.turn || "?")}</div>` : ""}
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
  $("#evidence-viewer").innerHTML = `<h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.source)} · recorded tool evidence</p><pre data-scroll-key="evidence:${escapeHtml(state.selected?.id)}:${escapeHtml(state.activeEvidence)}"><code>${escapeHtml(pretty(item.value))}</code></pre>`;
}

function renderTimeline() {
  document.querySelectorAll("#timeline details[data-timeline-id][open]").forEach(details => {
    state.expandedTimeline.add(details.dataset.timelineId);
  });
  const timeline = state.selected?.timeline || [];
  const cycles = recoveryCycles();
  if (!cycles.length && state.timelineFilter === "recovery") state.timelineFilter = "all";
  const isRecoveryEvent = item => item.kind === "worker"
    || ["Temporal retained the durable checkpoint", "Temporal replay restored Workflow context", "Temporal replay query pending", "SignalRoom reattached to the recovered case", "Temporal retained the unresolved external-effect Activity", "Temporal replayed the unresolved side-effect assignment"].includes(item.title)
    || (item.kind === "model" && (item.detail || "").includes("Replacement Worker resumed"));
  const filtered = state.timelineFilter === "recovery"
    ? timeline.filter(isRecoveryEvent)
    : state.timelineFilter === "agent"
      ? timeline.filter(item => ["model", "tool", "tool_result", "handoff"].includes(item.kind))
      : timeline;
  $("#timeline-filters").innerHTML = timeline.length ? [
    ["recovery", `Recovery${cycles.length ? ` · ${cycles.length}` : ""}`],
    ["agent", "Agent work"],
    ["all", `All events · ${timeline.length}`],
  ].map(([value, label]) => `<button class="${state.timelineFilter === value ? "active" : ""}" data-timeline-filter="${value}">${escapeHtml(label)}</button>`).join("") : "";
  const visibleKinds = [...new Set(filtered.map(item => item.kind || "unknown"))];
  const killedTitles = ["Agent Worker killed", "Agent Worker killed automatically in the result gap"];
  const recoveredTitles = ["Replacement Worker connected", "Replacement Worker resumed the external-effect Activity"];
  const hasKilledWorker = filtered.some(item => killedTitles.includes(item.title));
  const hasRecoveredWorker = filtered.some(item => recoveredTitles.includes(item.title));
  const hasGenericWorker = filtered.some(item => item.kind === "worker" && !killedTitles.includes(item.title) && !recoveredTitles.includes(item.title));
  $("#timeline-legend").innerHTML = Object.keys(timelineKindLabels)
    .flatMap(kind => {
      if (!visibleKinds.includes(kind)) return [];
      if (kind !== "worker") return [[kind, timelineKindLabels[kind]]];
      return [
        ...(hasGenericWorker ? [["worker", "Worker"]] : []),
        ...(hasKilledWorker ? [["worker-killed", "Worker killed"]] : []),
        ...(hasRecoveredWorker ? [["worker-recovered", "Worker recovered"]] : []),
      ];
    })
    .map(([kind, label]) => `<span data-kind="${escapeHtml(kind)}">${escapeHtml(label)}</span>`)
    .join("");
  if (!filtered.length) {
    $("#timeline").innerHTML = '<li class="empty">No transitions recorded.</li>';
    return;
  }
  $("#timeline").innerHTML = [...filtered].reverse().map(item => {
    const recoveryMarker = killedTitles.includes(item.title) ? "worker-killed" : recoveredTitles.includes(item.title) ? "worker-recovered" : "";
    return `<li class="${escapeHtml(item.status || "")} ${recoveryMarker}" data-kind="${escapeHtml(item.kind || "unknown")}">
    <time>${escapeHtml(clock(item.at))} · ${escapeHtml(item.kind)}</time>
    <strong>${escapeHtml(item.title)}</strong>
    <p>${escapeHtml(item.detail || "")}</p>
    ${item.data != null ? `<details data-timeline-id="${escapeHtml(item.id)}" ${state.expandedTimeline.has(item.id) ? "open" : ""}><summary>Inspect recorded payload</summary><pre data-scroll-key="timeline:${escapeHtml(state.selected?.id)}:${escapeHtml(item.id)}"><code>${escapeHtml(pretty(item.data))}</code></pre></details>` : ""}
  </li>`;
  }).join("");
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
  const viewport = captureScrollState();
  renderRuntime();
  renderReliability();
  renderCases();
  renderCase();
  renderPipeline();
  renderAgents();
  renderEvidence();
  renderRecoveryProof();
  renderTimeline();
  renderApproval();
  restoreScrollState(viewport);
}

async function refresh() {
  if (state.refreshInFlight) return;
  state.refreshInFlight = true;
  try {
    const [caseResponse, runtime, definitionResponse] = await Promise.all([
      api("/api/cases"), api("/api/runtime"), api("/api/agent-definitions"),
    ]);
    state.cases = caseResponse.cases || [];
    state.runtime = runtime;
    state.definitions = definitionResponse.agents || {};
    if (!state.selectedId && state.cases.length) state.selectedId = state.cases[0].id;
    const summary = state.cases.find(item => item.id === state.selectedId);
    state.selected = summary ? await api(`/api/cases/${encodeURIComponent(summary.id)}`) : null;
    const paymentCode = state.selected?.side_effect?.payment_code;
    state.provider = paymentCode
      ? await api(`/api/reliability/provider?payment_code=${encodeURIComponent(paymentCode)}`)
      : { status: state.runtime?.third_party?.status || "unknown", payments: [], audit: [] };
    state.providerRefreshedAt = new Date().toISOString();
    renderAll();
  } catch (error) {
    $("#runtime-indicator").textContent = "SignalRoom API unavailable";
    $("#header-dot").className = "status-dot offline";
  } finally {
    state.refreshInFlight = false;
  }
}

async function triggerCase() {
  if (state.busy) return;
  state.busy = true;
  const button = $("#hero-trigger");
  button.disabled = true;
  button.textContent = "Starting durable case…";
  try {
    const strategy = $("#repair-strategy").value;
    const response = await api("/api/lab/run", {
      method: "POST",
      body: JSON.stringify({
        step_delay: 0.42,
        side_effect_strategy: strategy,
        auto_crash: strategy !== "none" && $("#auto-crash").checked,
        hold_seconds: 20,
      }),
    });
    state.selectedId = response.case.id;
    state.activeEvidence = 0;
    state.activeStage = null;
    state.expandedTimeline.clear();
    toast(strategy === "none"
      ? "Durable repair case started."
      : "Durable repair started. The reviewed code will call the provider before merge approval.");
    await refresh();
    document.querySelector(".map-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.busy = false;
    button.disabled = false;
    button.innerHTML = 'Run the repair pipeline <span>↗</span>';
  }
}

async function killWorker() {
  if (state.busy) return;
  const caseTerminal = ["completed", "failed", "rejected"].includes(state.selected?.status);
  const activeCase = state.selected && !caseTerminal ? state.selected : null;
  if (!activeCase) return;
  state.busy = true;
  renderRuntime();
  try {
    const response = await api("/api/demo/worker/crash", {
      method: "POST",
      body: JSON.stringify({ case_id: activeCase?.id }),
    });
    toast(`Worker ${response.killed_worker_pid} killed. Watch the same case show Temporal replay and agent recovery.`);
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
    state.activeRecoveryId = null;
    state.recoveryContextOpen = false;
    state.caseContextOpen = false;
    state.timelineFilter = "recovery";
    state.expandedTimeline.clear();
    await refresh();
    return;
  }
  const recoveryButton = event.target.closest("[data-recovery-id]");
  if (recoveryButton) {
    state.activeRecoveryId = recoveryButton.dataset.recoveryId;
    renderRecoveryProof();
    renderAgents();
    return;
  }
  const timelineFilterButton = event.target.closest("[data-timeline-filter]");
  if (timelineFilterButton) {
    state.timelineFilter = timelineFilterButton.dataset.timelineFilter;
    renderTimeline();
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

$("#recovery-proof").addEventListener("toggle", event => {
  const details = event.target.closest?.("details[data-recovery-context]");
  if (!details) return;
  state.recoveryContextOpen = details.open;
}, true);

$("#case-content").addEventListener("toggle", event => {
  const details = event.target.closest?.("details[data-case-context]");
  if (!details) return;
  state.caseContextOpen = details.open;
}, true);

$("#reliability-timeline").addEventListener("toggle", event => {
  const details = event.target.closest?.("details[data-reliability-timeline-id]");
  if (!details) return;
  if (details.open) state.expandedExternalTimeline.add(details.dataset.reliabilityTimelineId);
  else state.expandedExternalTimeline.delete(details.dataset.reliabilityTimelineId);
}, true);

$("#hero-trigger").addEventListener("click", triggerCase);
$("#repair-strategy").addEventListener("change", event => {
  const enabled = event.target.value !== "none";
  $("#auto-crash").checked = enabled;
});
$("#auto-crash").addEventListener("change", event => {
  if (event.target.checked && $("#repair-strategy").value === "none") {
    $("#repair-strategy").value = "native_idempotency";
    toast("Auto-kill selected. The idempotent API result-gap scenario is now active.");
  }
});
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
