const state = {
  events: [], selected: null, mode: "shadow", runtime: "unknown",
  runtimeInfo: null, activeStage: null, demoRunning: false, demoAvailable: false,
};
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

const statusLabels = {
  queued: "Queued", validating: "Validating", awaiting_approval: "Approval needed",
  observing_outcome: "Awaiting outcome", completed: "Outcome recorded",
  shadowed: "Legacy shadow run", needs_approval: "Approval needed",
  acted: "Action dispatched", approved: "Approved", rejected: "Rejected",
};

const stageGuide = {
  event: ["Signal accepted", "The API validates the event contract and deduplicates it by durable Workflow ID."],
  context: ["Worker assembles context", "This is where the Temporal Worker first does meaningful agent work: Activities gather fresh source data."],
  policy: ["Policy sets the boundary", "Deterministic rules decide whether the proposal can proceed, must wait for a human, or stays in shadow mode."],
  action: ["Agent proposes an intervention", "A local rule or open Ollama model selects a bounded action. The recommendation is evidence, not yet an outcome."],
  human: ["Durable human exception", "Temporal can wait here for hours or days without a Worker process consuming compute. Approval arrives as a signal."],
  execution: ["Side effect executes once", "An idempotent Activity performs the approved action at most once, even if it is retried after a crash."],
  outcome: ["Source system reports evidence", "The Workflow remains open until the business system reports what actually happened. It can receive this signal while the Worker is offline."],
  feedback: ["Intervention is judged", "Operator feedback records whether the interruption was useful or noise and closes the learning loop."],
  queued: ["Temporal schedules work", "The API started a durable Workflow. Temporal now places its first Workflow Task on the SignalRoom task queue."],
};

const examples = {
  "workflow.stalled": "Payment reconciliation workflow has stalled for 61 minutes",
  "deploy.failed": "Checkout API deployment failed after the schema migration",
  "threshold.breached": "Cold-storage temperature threshold has been breached for 9 minutes",
  "document.uploaded": "A vendor agreement document containing regulated fields was uploaded",
  "support.sla_risk": "Enterprise support ticket is 23 minutes from its SLA deadline",
};

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 3200);
}

function relativeTime(value) {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" }[character]));
}

function renderEvents() {
  $("#event-count").textContent = String(state.events.length).padStart(2, "0");
  $("#event-list").innerHTML = state.events.map((event) => `
    <button class="event-row ${state.selected?.id === event.id ? "active" : ""}" data-id="${event.id}" data-status="${event.status}" role="listitem">
      <span class="event-pip"></span>
      <span><span class="event-title">${escapeHtml(event.summary)}</span><span class="event-source">${escapeHtml(event.source)} · ${event.id}</span></span>
      <time class="event-time" datetime="${event.received_at}">${relativeTime(event.received_at)}</time>
    </button>`).join("");
  $$(".event-row").forEach((row) => row.addEventListener("click", () => selectEvent(row.dataset.id)));
}

function inferredActiveStage(event, trace) {
  if (event.status === "queued") return "queued";
  if (event.status === "validating") return trace.some((item) => item.stage === "context") ? "context" : "event";
  if (["awaiting_approval", "needs_approval"].includes(event.status)) return "human";
  if (event.status === "observing_outcome") return "outcome";
  if (event.status === "completed" && !event.feedback?.rating) return "feedback";
  return null;
}

function renderPath(trace) {
  const visibleTrace = trace.length ? trace : [{ stage: "queued", state: "waiting", note: "Temporal workflow starting" }];
  const activeStage = state.activeStage || inferredActiveStage(state.selected, visibleTrace);
  $("#event-path").innerHTML = visibleTrace.map((item) => `
    <button class="path-node ${escapeHtml(item.state)} ${activeStage === item.stage ? "active" : ""}" data-stage="${escapeHtml(item.stage)}" aria-label="Inspect ${escapeHtml(item.stage)} stage">
      <span class="path-dot"></span><span class="path-stage">${escapeHtml(item.stage)}</span><span class="path-note">${escapeHtml(item.note)}</span>
    </button>`).join("");
  $$(".path-node").forEach((node) => node.addEventListener("click", () => {
    state.activeStage = node.dataset.stage;
    renderPath(trace);
    renderStageInspector(visibleTrace.find((item) => item.stage === state.activeStage));
  }));
  renderStageInspector(visibleTrace.find((item) => item.stage === activeStage) || visibleTrace[0]);
}

function renderStageInspector(item) {
  if (!item) return;
  const guide = stageGuide[item.stage] || [item.stage, "This stage is recorded in the durable event history."];
  $("#path-inspector").innerHTML = `
    <span class="inspector-index">${escapeHtml(item.stage)}</span>
    <div><strong>${escapeHtml(guide[0])}</strong><p>${escapeHtml(guide[1])}<br>Record: ${escapeHtml(item.note || "No note")}</p></div>
    <span class="inspector-state">${escapeHtml(item.state)}</span>`;
}

function renderSelected() {
  const event = state.selected;
  if (!event) return;
  const decision = event.decision || {};
  const validation = event.validation || { valid: true };
  const recommendationAvailable = Boolean(decision.label);
  $("#decision-empty").hidden = true;
  $("#decision-content").hidden = false;
  $("#decision-status").textContent = statusLabels[event.status] || event.status;
  $("#decision-status").dataset.status = event.status;
  $("#decision-type").textContent = `${event.type} · ${event.id}`;
  $("#decision-label").textContent = recommendationAvailable ? decision.label : validation.valid === false ? "Signal rejected before agent execution" : "Durable workflow is assembling the case";
  $("#decision-rationale").textContent = recommendationAvailable ? decision.rationale : validation.issue || "The Worker is validating the event and gathering context.";
  $("#decision-confidence").textContent = recommendationAvailable ? `${Math.round(decision.confidence * 100)}%` : "—";
  $("#decision-risk").textContent = decision.risk || "—";
  $("#decision-owner").textContent = decision.owner || "—";
  $("#decision-model").textContent = `${event.model_mode || "pending"} · ${decision.execution_engine || "local"}`;
  $("#context-chips").innerHTML = (event.context?.snapshot || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("") || "<span>Context pending</span>";
  $("#selected-meta").textContent = `${event.summary} · ${event.source}`;

  const approval = $("#approval-box");
  const needsApproval = ["awaiting_approval", "needs_approval"].includes(event.status);
  approval.dataset.hidden = needsApproval ? "false" : "true";
  const actionResult = event.outcome?.action_result;
  $("#approval-copy").textContent = needsApproval
    ? "Temporal is durably paused. The Worker is free to do other work until a decision arrives."
    : actionResult?.state === "withheld_shadow" ? "Shadow mode withheld the side effect; the Workflow is still observing the business result."
      : actionResult?.state === "dispatched" ? `Action dispatched once as ${actionResult.reference}.`
        : validation.valid === false ? validation.issue : "No human exception is currently pending.";

  renderOutcome(event);
  $$(".feedback-row button").forEach((button) => button.classList.toggle("active", event.feedback?.rating === button.dataset.rating));
  renderPath(decision.trace || []);
}

function renderOutcome(event) {
  const outcome = event.outcome || {};
  const contract = outcome.contract;
  const result = outcome.business_result;
  const resultBox = $("#outcome-result");
  const reportBox = $("#outcome-report");
  if (!contract) {
    $("#outcome-question").textContent = result?.note || "This signal was rejected before an outcome contract was created.";
    $("#outcome-state").textContent = result?.label || "Not applicable";
    reportBox.hidden = true;
    resultBox.hidden = true;
    return;
  }
  $("#outcome-question").textContent = contract.question;
  $("#outcome-metric-label").textContent = contract.metric_label;
  if (result) {
    $("#outcome-state").textContent = result.success ? "Successful" : "Unsuccessful";
    const metricValue = result.metrics?.[contract.metric];
    resultBox.innerHTML = `<strong>${escapeHtml(result.label)}</strong><br>${escapeHtml(result.note || "Outcome evidence recorded.")}${metricValue !== undefined ? `<br>${escapeHtml(contract.metric_label)}: ${escapeHtml(metricValue)}` : ""}`;
    resultBox.className = `outcome-result ${result.success ? "success" : "failure"}`;
    resultBox.hidden = false;
    reportBox.hidden = true;
  } else {
    $("#outcome-state").textContent = event.status === "observing_outcome" ? "Awaiting evidence" : "Not ready";
    resultBox.hidden = true;
    reportBox.hidden = event.status !== "observing_outcome";
    $("#outcome-choices").innerHTML = contract.choices.map((choice) => `<button data-code="${choice.code}">${escapeHtml(choice.label)}</button>`).join("");
    $$("#outcome-choices button").forEach((button) => button.addEventListener("click", () => reportOutcome(button.dataset.code, contract.metric)));
  }
}

function renderRuntime(runtime) {
  state.runtimeInfo = runtime;
  const worker = runtime.worker || {};
  $("#temporal-status").textContent = runtime.temporal_connected ? "Connected" : "Offline";
  $("#worker-status").textContent = worker.status || "unknown";
  $("#worker-status").dataset.status = worker.status || "unknown";
  $("#worker-detail").textContent = worker.worker_pid ? `PID ${worker.worker_pid} · runs workflow tasks` : "No Worker process; Temporal keeps state";
  const count = worker.restart_count || 0;
  $("#restart-count").textContent = `${count} restart${count === 1 ? "" : "s"}`;
  $("#runtime-indicator").textContent = runtime.temporal_connected ? `Temporal connected · Worker ${worker.status || "unknown"}` : "Local fallback runtime";
  state.demoAvailable = Boolean(runtime.temporal_connected && runtime.chaos_demo_enabled && worker.status === "online");
  const demoButton = $("#durability-demo-button");
  demoButton.disabled = state.demoRunning;
  demoButton.classList.toggle("demo-unavailable", !state.demoAvailable);
  demoButton.innerHTML = state.demoAvailable
    ? '<span class="demo-button-dot"></span>Run crash + resume demo'
    : '<span class="demo-button-dot"></span>Why is the demo unavailable?';
}

function selectEvent(id) {
  state.selected = state.events.find((event) => event.id === id);
  state.activeStage = null;
  renderEvents();
  renderSelected();
}

async function load() {
  const [{ events, mode, runtime }, metrics, runtimeInfo] = await Promise.all([api("/api/events"), api("/api/metrics"), api("/api/runtime")]);
  state.events = events;
  state.mode = mode;
  state.runtime = runtime;
  $("#run-mode").value = mode;
  if (!state.selected || !events.some((event) => event.id === state.selected.id)) state.selected = events[0];
  else state.selected = events.find((event) => event.id === state.selected.id);
  renderRuntime(runtimeInfo);
  renderEvents();
  renderSelected();
  renderMetrics(metrics);
}

function renderMetrics(metrics) {
  $("#metric-completed").textContent = metrics.completed;
  $("#metric-successful").textContent = metrics.successful;
  $("#metric-pending").textContent = metrics.pending;
  $("#metric-failed").textContent = metrics.failed;
  $("#outcome-score").textContent = metrics.completed ? `${Math.round(metrics.successful / metrics.completed * 100)}%` : "—";
}

function setDemoSteps(steps, activeIndex = -1, failedIndex = -1) {
  $("#demo-steps").innerHTML = steps.map((step, index) => `<li class="${index < activeIndex ? "done" : index === activeIndex ? "active" : index === failedIndex ? "failed" : ""}">${escapeHtml(step)}</li>`).join("");
}

async function runDurabilityDemo() {
  if (state.demoRunning) return;
  if (!state.demoAvailable) {
    const consoleBox = $("#demo-console");
    consoleBox.hidden = false;
    $("#demo-console-status").textContent = "Temporal runtime unavailable";
    const requirements = [
      "Stop the fallback server currently using port 4173",
      "Run ./scripts/dev.sh from the SignalRoom directory",
      "Wait for Temporal connected and Agent Worker online",
      "Click Run crash + resume demo again",
    ];
    setDemoSteps(requirements, -1, 0);
    showToast("Crash recovery requires Temporal and the supervised Worker. Start SignalRoom with ./scripts/dev.sh.");
    return;
  }
  state.demoRunning = true;
  const consoleBox = $("#demo-console");
  consoleBox.hidden = false;
  const steps = [
    "Start a durable Workflow and wait at the Outcome stage",
    "Kill the current Agent Worker process",
    "Send business outcome while no Worker is running",
    "Supervisor starts a replacement Worker",
    "Replacement Worker replays history and completes the same Workflow",
  ];
  setDemoSteps(steps, 0);
  $("#demo-console-status").textContent = "Temporal creating workflow";
  renderRuntime(state.runtimeInfo);
  let activeIndex = 0;
  try {
    const baselineRestarts = state.runtimeInfo?.worker?.restart_count || 0;
    const created = await api("/api/demo/durability", { method: "POST", body: "{}" });
    state.selected = created.event;
    state.activeStage = "outcome";
    await load();
    activeIndex = 1;
    setDemoSteps(steps, activeIndex);
    $("#demo-console-status").textContent = "Crashing Worker";

    await api("/api/demo/worker/crash", { method: "POST", body: "{}" });
    await sleep(350);
    const offlineRuntime = await api("/api/runtime");
    renderRuntime(offlineRuntime);
    activeIndex = 2;
    setDemoSteps(steps, activeIndex);
    $("#demo-console-status").textContent = "Worker offline · signal accepted by Temporal";

    await api(`/api/events/${created.event.id}/outcome`, {
      method: "POST",
      body: JSON.stringify({ code: "workflow_resumed", note: "Outcome signal was accepted while the Agent Worker was offline.", metrics: { recovery_time_minutes: 0 } }),
    });

    activeIndex = 3;
    setDemoSteps(steps, activeIndex);
    $("#demo-console-status").textContent = "Waiting for replacement Worker";
    const deadline = Date.now() + 16000;
    let replacementSeen = false;
    let completed = false;
    while (Date.now() < deadline) {
      await sleep(500);
      await load();
      replacementSeen = state.runtimeInfo?.worker?.status === "online" && (state.runtimeInfo?.worker?.restart_count || 0) > baselineRestarts;
      completed = state.selected?.id === created.event.id && state.selected?.status === "completed";
      if (replacementSeen && activeIndex === 3) {
        activeIndex = 4;
        setDemoSteps(steps, activeIndex);
        $("#demo-console-status").textContent = "Worker replaying history";
      }
      if (replacementSeen && completed) break;
    }
    if (!replacementSeen || !completed) throw new Error("The replacement Worker did not complete the proof before the demo timeout.");
    state.activeStage = "outcome";
    renderSelected();
    setDemoSteps(steps, 5);
    $("#demo-console-status").textContent = "Proof complete";
    showToast("Durability proven: a replacement Worker completed the same Workflow.");
  } catch (error) {
    setDemoSteps(steps, -1, activeIndex);
    $("#demo-console-status").textContent = "Demo stopped";
    showToast(error.message);
  } finally {
    state.demoRunning = false;
    const latestRuntime = await api("/api/runtime").catch(() => state.runtimeInfo);
    if (latestRuntime) renderRuntime(latestRuntime);
  }
}

async function decide(action) {
  if (!state.selected) return;
  await api(`/api/events/${state.selected.id}/${action}`, { method: "POST", body: "{}" });
  showToast(action === "approve" ? "Approval signal delivered to the Workflow." : "Rejection signal delivered to the Workflow.");
  await load();
}

async function reportOutcome(code, metric) {
  if (!state.selected) return;
  const metricValue = Number($("#outcome-metric").value);
  await api(`/api/events/${state.selected.id}/outcome`, { method: "POST", body: JSON.stringify({ code, note: $("#outcome-note").value, metrics: { [metric]: metricValue } }) });
  showToast("Business outcome delivered; the durable Workflow completed.");
  await load();
}

async function rate(rating) {
  if (!state.selected) return;
  await api(`/api/events/${state.selected.id}/feedback`, { method: "POST", body: JSON.stringify({ rating }) });
  showToast(rating === "useful" ? "Marked as a useful intervention." : "Marked as a false interruption.");
  await load();
}

$("#durability-demo-button").addEventListener("click", runDurabilityDemo);
$("#new-event-button").addEventListener("click", () => $("#event-dialog").showModal());
$("#close-dialog").addEventListener("click", () => $("#event-dialog").close());
$("#cancel-dialog").addEventListener("click", () => $("#event-dialog").close());
$("#approve-button").addEventListener("click", () => decide("approve"));
$("#reject-button").addEventListener("click", () => decide("reject"));
$$(".feedback-row button").forEach((button) => button.addEventListener("click", () => rate(button.dataset.rating)));
$("#run-mode").addEventListener("change", async (event) => {
  await api("/api/config", { method: "POST", body: JSON.stringify({ run_mode: event.target.value }) });
  showToast(`New Workflows will run in ${event.target.value} mode.`);
  await load();
});
$("#event-form select[name='type']").addEventListener("change", (event) => {
  $("#event-form textarea[name='summary']").value = examples[event.target.value];
});
$("#event-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const result = await api("/api/events", { method: "POST", body: JSON.stringify({ type: form.get("type"), source: form.get("source"), summary: form.get("summary"), payload: { injected: true } }) });
  $("#event-dialog").close();
  state.selected = result.event;
  state.activeStage = null;
  showToast(result.duplicate ? "Duplicate Workflow already exists." : `Event started on the ${result.runtime} runtime.`);
  await load();
});

load().catch((error) => showToast(error.message));
setInterval(() => {
  if (!state.demoRunning && !document.hidden) load().catch(() => {});
}, 3000);
