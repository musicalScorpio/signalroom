const state = { events: [], selected: null, mode: "shadow", runtime: "unknown" };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const statusLabels = {
  queued: "Queued",
  validating: "Validating",
  awaiting_approval: "Approval needed",
  observing_outcome: "Awaiting outcome",
  completed: "Outcome recorded",
  shadowed: "Legacy shadow run",
  needs_approval: "Approval needed",
  acted: "Action dispatched",
  approved: "Approved",
  rejected: "Rejected",
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
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2800);
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
    </button>
  `).join("");
  $$(".event-row").forEach((row) => row.addEventListener("click", () => selectEvent(row.dataset.id)));
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
  $("#decision-rationale").textContent = recommendationAvailable ? decision.rationale : validation.issue || "The workflow is validating the event and gathering context.";
  $("#decision-confidence").textContent = recommendationAvailable ? `${Math.round(decision.confidence * 100)}%` : "—";
  $("#decision-risk").textContent = decision.risk || "—";
  $("#decision-owner").textContent = decision.owner || "—";
  $("#decision-model").textContent = `${event.model_mode || "pending"} · ${decision.execution_engine || "local"}`;
  $("#context-chips").innerHTML = (event.context?.snapshot || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("") || "<span>Context pending</span>";
  $("#selected-meta").textContent = `${event.summary} · ${event.source}`;

  const approval = $("#approval-box");
  const needsApproval = event.status === "awaiting_approval" || event.status === "needs_approval";
  approval.dataset.hidden = needsApproval ? "false" : "true";
  const actionResult = event.outcome?.action_result;
  $("#approval-copy").textContent = needsApproval
    ? "The durable workflow is paused. It can wait through restarts until a decision arrives."
    : actionResult?.state === "withheld_shadow"
      ? "Shadow mode withheld the side effect; the workflow is still observing the business result."
      : actionResult?.state === "dispatched"
        ? `Action dispatched once as ${actionResult.reference}.`
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

function renderPath(trace) {
  $("#event-path").innerHTML = trace.length ? trace.map((item) => `
    <div class="path-node ${item.state}"><span class="path-dot"></span><span class="path-stage">${escapeHtml(item.stage)}</span><span class="path-note">${escapeHtml(item.note)}</span></div>
  `).join("") : `<div class="path-node waiting"><span class="path-dot"></span><span class="path-stage">queued</span><span class="path-note">Temporal workflow starting</span></div>`;
}

function selectEvent(id) {
  state.selected = state.events.find((event) => event.id === id);
  renderEvents();
  renderSelected();
}

async function load() {
  const [{ events, mode, runtime }, metrics] = await Promise.all([api("/api/events"), api("/api/metrics")]);
  state.events = events;
  state.mode = mode;
  state.runtime = runtime;
  $("#run-mode").value = mode;
  $("#runtime-indicator").textContent = runtime === "temporal" ? "Temporal durable runtime" : "Local fallback runtime";
  if (!state.selected || !events.some((event) => event.id === state.selected.id)) state.selected = events[0];
  else state.selected = events.find((event) => event.id === state.selected.id);
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

async function decide(action) {
  if (!state.selected) return;
  await api(`/api/events/${state.selected.id}/${action}`, { method: "POST", body: JSON.stringify({}) });
  showToast(action === "approve" ? "Approval signal delivered to the workflow." : "Rejection signal delivered to the workflow.");
  await load();
}

async function reportOutcome(code, metric) {
  if (!state.selected) return;
  const metricValue = Number($("#outcome-metric").value);
  await api(`/api/events/${state.selected.id}/outcome`, {
    method: "POST",
    body: JSON.stringify({ code, note: $("#outcome-note").value, metrics: { [metric]: metricValue } }),
  });
  showToast("Business outcome delivered; the durable workflow completed.");
  await load();
}

async function rate(rating) {
  if (!state.selected) return;
  await api(`/api/events/${state.selected.id}/feedback`, { method: "POST", body: JSON.stringify({ rating }) });
  showToast(rating === "useful" ? "Marked as a useful intervention." : "Marked as a false interruption.");
  await load();
}

$("#new-event-button").addEventListener("click", () => $("#event-dialog").showModal());
$("#close-dialog").addEventListener("click", () => $("#event-dialog").close());
$("#cancel-dialog").addEventListener("click", () => $("#event-dialog").close());
$("#approve-button").addEventListener("click", () => decide("approve"));
$("#reject-button").addEventListener("click", () => decide("reject"));
$$(".feedback-row button").forEach((button) => button.addEventListener("click", () => rate(button.dataset.rating)));
$("#run-mode").addEventListener("change", async (event) => {
  await api("/api/config", { method: "POST", body: JSON.stringify({ run_mode: event.target.value }) });
  showToast(`New workflows will run in ${event.target.value} mode.`);
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
  showToast(result.duplicate ? "Duplicate workflow already exists." : `Event started on the ${result.runtime} runtime.`);
  await load();
});

load().catch((error) => showToast(error.message));
