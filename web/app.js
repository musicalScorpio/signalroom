const state = { events: [], selected: null, mode: "shadow" };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const statusLabels = {
  shadowed: "Observed only",
  needs_approval: "Approval needed",
  acted: "Action dispatched",
  approved: "Approved",
  rejected: "Rejected",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
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
  $("#decision-empty").hidden = true;
  $("#decision-content").hidden = false;
  $("#decision-status").textContent = statusLabels[event.status] || event.status;
  $("#decision-status").dataset.status = event.status;
  $("#decision-type").textContent = `${event.type} · ${event.id}`;
  $("#decision-label").textContent = event.decision.label;
  $("#decision-rationale").textContent = event.decision.rationale;
  $("#decision-confidence").textContent = `${Math.round(event.decision.confidence * 100)}%`;
  $("#decision-risk").textContent = event.decision.risk;
  $("#decision-owner").textContent = event.decision.owner;
  $("#decision-model").textContent = event.model_mode;
  $("#context-chips").innerHTML = event.context.snapshot.map((item) => `<span>${escapeHtml(item)}</span>`).join("");
  $("#selected-meta").textContent = `${event.summary} · ${event.source}`;

  const approval = $("#approval-box");
  const needsApproval = event.status === "needs_approval";
  approval.dataset.hidden = needsApproval ? "false" : "true";
  $("#approval-copy").textContent = needsApproval
    ? "This action crosses the delegated authority boundary."
    : event.status === "shadowed"
      ? "Shadow mode withheld the side effect while preserving the recommendation."
      : `The exception is ${event.status}; the outcome is preserved in the ledger.`;

  $$(".feedback-row button").forEach((button) => button.classList.toggle("active", event.feedback.rating === button.dataset.rating));
  renderPath(event.decision.trace);
}

function renderPath(trace) {
  $("#event-path").innerHTML = trace.map((item) => `
    <div class="path-node ${item.state}">
      <span class="path-dot"></span>
      <span class="path-stage">${escapeHtml(item.stage)}</span>
      <span class="path-note">${escapeHtml(item.note)}</span>
    </div>
  `).join("");
}

function selectEvent(id) {
  state.selected = state.events.find((event) => event.id === id);
  renderEvents();
  renderSelected();
}

async function load() {
  const [{ events, mode }, metrics] = await Promise.all([api("/api/events"), api("/api/metrics")]);
  state.events = events;
  state.mode = mode;
  $("#run-mode").value = mode;
  if (!state.selected || !events.some((event) => event.id === state.selected.id)) state.selected = events[0];
  else state.selected = events.find((event) => event.id === state.selected.id);
  renderEvents();
  renderSelected();
  renderMetrics(metrics);
}

function renderMetrics(metrics) {
  $("#metric-useful").textContent = metrics.useful;
  $("#metric-noise").textContent = metrics.noise;
  $("#metric-escalations").textContent = metrics.escalations;
  $("#metric-shadowed").textContent = metrics.shadowed;
  const rated = metrics.useful + metrics.noise;
  $("#utility-score").textContent = rated ? `${Math.round(metrics.useful / rated * 100)}%` : "—";
}

async function decide(action) {
  if (!state.selected) return;
  await api(`/api/events/${state.selected.id}/${action}`, { method: "POST", body: JSON.stringify({}) });
  showToast(action === "approve" ? "Action approved and recorded." : "Action rejected and recorded.");
  await load();
}

async function rate(rating) {
  if (!state.selected) return;
  await api(`/api/events/${state.selected.id}/feedback`, { method: "POST", body: JSON.stringify({ rating }) });
  showToast(rating === "useful" ? "Marked as a useful intervention." : "Marked as a false interruption.");
  await load();
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" }[character]));
}

$("#new-event-button").addEventListener("click", () => $("#event-dialog").showModal());
$("#close-dialog").addEventListener("click", () => $("#event-dialog").close());
$("#cancel-dialog").addEventListener("click", () => $("#event-dialog").close());
$("#approve-button").addEventListener("click", () => decide("approve"));
$("#reject-button").addEventListener("click", () => decide("reject"));
$$(".feedback-row button").forEach((button) => button.addEventListener("click", () => rate(button.dataset.rating)));
$("#run-mode").addEventListener("change", async (event) => {
  await api("/api/config", { method: "POST", body: JSON.stringify({ run_mode: event.target.value }) });
  showToast(`New events will run in ${event.target.value} mode.`);
  await load();
});
$("#event-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const result = await api("/api/events", {
    method: "POST",
    body: JSON.stringify({ type: form.get("type"), source: form.get("source"), summary: form.get("summary"), payload: { injected: true } }),
  });
  $("#event-dialog").close();
  state.selected = result.event;
  showToast(result.duplicate ? "Duplicate event ignored." : "Event accepted and evaluated.");
  await load();
});

load().catch((error) => showToast(error.message));
