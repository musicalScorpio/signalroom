# SignalRoom

**The open-source control room for durable, event-driven ambient agents.**

SignalRoom turns a system signal into supervised, recoverable agent work. Instead of waiting for a person to open a chat, it detects that something changed, assembles evidence, routes bounded agents, pauses for a human decision when risk demands it, and verifies whether the intervention produced the intended outcome.

This repository is a fully local reference implementation built with Temporal, Ollama, Python, SQLite, and Git. It demonstrates one unified software-repair and external-effect recovery case—not a scripted animation and not a chatbot wrapped around a model.

~~~text
event → context → policy → agent work → human exception → verified outcome → evidence
~~~

~~~mermaid
flowchart LR
    signal[System signal] --> temporal[Temporal history]
    temporal --> agents[Investigate and repair]
    agents --> proof[Review and external-effect proof]
    proof --> human[Human merge gate]
    human --> outcome[Merge and verified outcome]
    temporal -. "replay unfinished work after crash" .-> agents
    temporal -. "restore operation identity and policy" .-> proof
~~~

## Why SignalRoom exists

The next agent interface may be an event rather than a screen:

- a deployment breaks a contract;
- a threshold is breached;
- a workflow stalls;
- a document arrives;
- a database record changes.

The hard production problem is no longer only whether a model can answer. It is whether the system can notice the right moment, preserve execution across failure, constrain agent behavior, involve a person at the correct boundary, and prove that the action helped.

SignalRoom makes that entire **event-to-outcome path** visible.

## What the demonstration proves

The included scenario begins when Inventory Service contract v2 breaks checkout. SignalRoom then runs an evidence-driven repair case:

1. A runtime failure becomes a durable case.
2. Two investigation agents independently reproduce the failure and inspect contract history.
3. The Repair Agent sends the evidence and bounded repository contents to Ollama, which diagnoses the defect and authors the replacement source.
4. An Independent Review Agent inspects the exact diff and reruns the tests.
5. If an API mode is selected, the Side-Effect Execution Agent runs the exact reviewed branch against the separate local provider.
6. An optional automatic fault kills the Worker after the provider commits but before Temporal records the Activity result.
7. Temporal restores the operation identity and selected retry policy on a replacement Worker; the agent retries safely, stops, or reconciles with GET.
8. Temporal waits durably for a human to approve the exact commit SHA and provider evidence.
9. An idempotent Activity merges only the reviewed commit, then the Outcome Verification Agent retests `main` without repeating the live side effect.

A case is successful only when the original checkout request succeeds on merged code and the regression suite passes. “The agent finished” is not accepted as a business outcome.

You can kill the Agent Worker at any point. The process loses its in-memory state, the supervisor starts a replacement, and Temporal reconstructs the Workflow from Event History. The case timeline shows the Worker death, retained durable checkpoint, replacement PID, replayed context, and resumed stage.

## Architecture

[View the standalone Mermaid source](docs/signalroom-architecture.mmd).

The compact diagram above is the operator-level view. The standalone source shows the detailed local runtime, evidence stores, model, Git lab, and provider boundary.

### What each part owns

| Component | Owns | Does not own |
| --- | --- | --- |
| SignalRoom API | Event intake, case creation, operator endpoints, Temporal client calls | Workflow scheduling or agent reasoning |
| Temporal Server | Workflow Event History, task queues, retries, durable waits, Signals | Deciding whether a patch is correct |
| Temporal Worker | Workflow and Activity execution, child Agent Workflows, tool dispatch | Durable history after the process dies |
| Agent harness | Context assembly, command validation, tool schemas, path restrictions, syntax checks, and turn limits | Authoring the repair |
| Ollama model | Failure diagnosis, tool selection, and the complete replacement source | Orchestration, persistence, direct file access, or authority to merge |
| SQLite | Fast operator-facing projection of cases, evidence, agents, and timeline | Workflow recovery |
| Per-case Git lab | Source-of-truth branch, commit, diff, tests, merge, and verification artifacts | Temporal control flow |
| Worker supervisor | Local process monitoring and respawn | Workflow replay |
| Demo payment service | Provider-side key replay, deliberately non-idempotent writes, payment records, and request audit | SignalRoom orchestration or Temporal history |

### How Temporal actually helps

SignalRoom never writes directly to a queue. Its Temporal client starts or Signals a Workflow over gRPC. Temporal records that command in Workflow Event History and schedules Workflow or Activity tasks on the `signalroom-events` task queue.

Workers poll that queue. If the active Worker is killed:

1. Temporal still retains the Workflow history, completed Activity results, pending human wait, and Signal state.
2. The local supervisor starts a replacement Worker process.
3. The replacement polls the same task queue.
4. Temporal replays history to reconstruct deterministic Workflow state.
5. Activity completions already recorded in Event History are not repeated. If an external write happened but its Activity result was never recorded, Temporal applies the recorded retry policy: retry with the same idempotency key, stop after one attempt, or reconcile with a safe GET.

The supervisor resurrects the process. **Temporal resurrects the work.**

## Case-bound external-effect proof

The control room includes a separate, demo-only payment service under `third_party_service/`. It runs as another local process and owns `.signalroom/data/third_party.db`; SignalRoom cannot share a transaction with it. The hard-coded payment payload never moves real money.

The **Side-Effect Execution Agent** runs the reviewed application code under a durable policy. Temporal does not make an API idempotent; it restores the same reviewed commit, operation identity, and retry rule after process death. Provider rows remain the authority for whether the external effect exists.

Choose one of four modes before starting the repair case:

| Strategy | Write behavior | Recovery after Worker death |
| --- | --- | --- |
| Code repair only | No external API call | Proves model, Git, review, merge, and Worker recovery only |
| Idempotent API · POST retry | Reviewed checkout code sends POST with one stable operation key | Provider replays the original receipt and creates no duplicate |
| Non-idempotent API · `maximumAttempts=1` | One total POST attempt and zero write retries | Replacement agent is forbidden from calling create again; it reports an uncertain outcome |
| Non-idempotent API · zero POST retries + GET recovery | One unsafe POST attempt | Agent suppresses another create, repeats only safe GET checks after 1, 2, and 4 seconds, and adopts the payment it finds |

For API modes, **Auto-kill Worker in result gap** is selected by default. SignalRoom kills the Worker after the provider commits but before Temporal records the Activity result. The same case history then shows the failed process, retained Temporal assignment, replacement Worker, replay decision, request headers, provider rows, and request audit. Manual Worker kill remains available in the header.

A heartbeat is progress evidence, not proof of an external commit. If a provider supports neither a stable idempotency key nor a reliable lookup, SignalRoom stops at `uncertain`; it never claims that client-side state can manufacture exactly-once execution.

## Six bounded agents

Agents are versioned definitions in [backend/agents/definitions.json](backend/agents/definitions.json), not hard-coded personas.

| Agent | Responsibility | Allowed tools |
| --- | --- | --- |
| Reproduction Agent | Reproduce the failure and establish a factual signature | `tests.reproduce`, `logs.read` |
| Change Intelligence Agent | Identify contract drift and affected code paths | `contract.inspect`, `git.history` |
| Repair Agent | Inspect the bounded repository, author source with Ollama, test it, and commit the smallest compatible fix | `code.inspect`, `code.patch`, `tests.run`, `git.commit` |
| Side-Effect Execution Agent | Execute the reviewed checkout under the selected write/reconciliation policy | `payments.create_idempotent`, `payments.create_once`, `payments.lookup` |
| Independent Review Agent | Review the exact commit and independently rerun tests | `review.diff`, `tests.run` |
| Outcome Verification Agent | Verify merged `main` against the original failure | `verify.main` |

Each definition includes:

- a purpose and system prompt;
- an explicit tool allowlist;
- evidence-producing tools that must run before completion;
- a maximum turn count;
- a version and UI identity.

The model never receives a general-purpose shell. It returns a structured JSON command, the harness validates it, and a typed Temporal Activity executes the corresponding scoped tool. Read-only agents can use the deterministic policy in degraded test mode. The Repair Agent cannot: it requires valid Ollama-authored source and stops rather than substituting a predefined fix.

### Where the repair code comes from

There is no fixed solution inside `code.patch`. The repair loop is:

1. `code.inspect` returns the allowlisted source, dependency, tests, and runtime log.
2. Ollama diagnoses the failure and calls `code.patch` with a complete `replacement_content` value that it authored.
3. The patch tool restricts the target path, enforces a size limit, compiles the proposed Python, applies it on an isolated branch, and records its SHA-256 and diff.
4. `tests.run` returns real test output to Ollama.
5. If tests fail, Ollama may author another replacement and test again.
6. `git.commit` is permitted only when the latest test run passed after the latest patch.

Temporal records every model command and tool result, so a replacement Worker can resume the loop without losing completed evidence.

Local model turns are intentionally concise. `gpt-oss` defaults to low reasoning; read-only turns default to a 512-token output budget and repair generation to 4,096 tokens so there is room for the complete source file. Set `OLLAMA_THINK` or `OLLAMA_NUM_PREDICT` to override those defaults.

## Human authority and evidence

The human approval is not ceremonial. The UI presents:

- the reproduced failure;
- contract and Git history evidence;
- the isolated repair branch;
- the exact commit SHA and diff;
- independent review findings;
- passing test output;
- the selected external-effect policy, provider receipt, replay or reconciliation result, and duplicate count.

Approval is sent to the waiting Workflow as a Temporal Signal. The merge Activity verifies that the approved SHA is still the reviewed SHA before changing `main`. A separate agent then measures the result.

This gives every run a reconstructable record:

~~~text
signal → source context → model command → tool evidence → handoff
       → provider proof → recovery decision → approval Signal
       → exact merge → outcome verification
~~~

## Tech stack

| Layer | Technology |
| --- | --- |
| Control room | Vanilla HTML, CSS, and JavaScript |
| Local API | Python 3.11 standard-library HTTP server |
| Durable orchestration | Temporal Server and Temporal Python SDK |
| Local inference | Ollama with gpt-oss or another installed JSON-capable open-weight model |
| Agent runtime | Python harness, versioned JSON definitions, typed tool contracts |
| Projection store | SQLite |
| Demo external boundary | Python standard-library HTTP service with a separate SQLite database |
| Repair environment | Git and Python `unittest` |
| Local infrastructure | Docker or Colima, `uv`, and pip-compatible requirements |
| Browser testing | Playwright |

Everything runs locally. No hosted model or proprietary agent platform is required.

## Quick start

### Requirements

- Python 3.11 or newer
- Docker Desktop or Colima
- [uv](https://github.com/astral-sh/uv)
- [Ollama](https://github.com/ollama/ollama) with a local JSON-capable model — required for real agent reasoning

### Clone and run

~~~bash
git clone https://github.com/musicalScorpio/signalroom.git
cd signalroom

ollama pull gpt-oss:20b
./scripts/start.sh
~~~

SignalRoom prefers an explicitly configured model when it is installed, then the model currently loaded by Ollama, then the first installed model. To force another locally installed model:

~~~bash
OLLAMA_MODEL=gpt-oss:20b ./scripts/start.sh
~~~

Open:

- SignalRoom: [http://127.0.0.1:4173](http://127.0.0.1:4173)
- Temporal Web UI: [http://127.0.0.1:8233](http://127.0.0.1:8233)
- Demo third-party API: [http://127.0.0.1:4180/health](http://127.0.0.1:4180/health)

The launcher is the single entry point for the complete local stack. It starts or reuses Docker/Colima, Temporal, and Ollama; synchronizes Python dependencies; initializes both SQLite schemas; starts the separate demo provider, supervised Worker, API, and UI; and waits for every readiness check before reporting success. `./scripts/dev.sh` remains as a backward-compatible alias.

Runtime logs are written under `.signalroom/logs/`. Press `Ctrl+C` in the launcher terminal to stop processes owned by that run. The Temporal container remains online intentionally so its durable history is available on the next start.

Ollama is the reasoning and code-generation layer. If Ollama is unavailable, read-only workflow mechanics can still be exercised in degraded test mode, but the Repair Agent will not create or commit a patch. SignalRoom never substitutes placeholder repair code.

For a conventional pip environment:

~~~bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
~~~

## Run the unified repair and recovery demonstration

1. Choose an **External-effect proof** mode. Use **Code repair only** for the base workflow, or one of the three API safety policies to include the provider boundary.
2. Leave **Auto-kill Worker in result gap** selected for an API mode. The app will create the external effect, then kill the Worker before Temporal receives the Activity result. You can also use the header's **Kill Agent Worker** control at any non-terminal stage.
3. Select **Run the repair pipeline** once. This is the only start control.
4. Follow the interactive Event-to-Outcome Map while the investigation, repair, review, and side-effect agents work. Agent cards and the case timeline remain synchronized with the active stage.
5. Expand tool results, recovery context, and API decisions to inspect the exact prompt/tool contract, generated source, diff, tests, operation identity, request, and provider response.
6. After a crash, confirm that the timeline records the killed Worker, Temporal's retained assignment, the replacement PID, the restored context, and the policy decision. The case must resume from the unresolved step—not restart from investigation.
7. For an API mode, compare **Provider truth vs. Temporal knowledge**. This panel refreshes automatically from the provider's separate database.
8. At the human gate, inspect the exact reviewed commit and all attached evidence, then select **Merge reviewed commit**.
9. Wait for the Outcome Verification Agent and the green **Outcome reached** state.

Expected external-effect evidence:

| Mode | Provider audit | Final interpretation |
| --- | --- | --- |
| Idempotent API | Two POST requests, one payment row | The retry reused the stable key and the provider replayed the first receipt |
| `maximumAttempts=1` | One POST request, one payment row | No duplicate is possible, but Temporal honestly reports an uncertain result because the receipt was lost |
| GET recovery | One POST, then repeatable GET checks, one payment row | The replacement agent suppresses a second create and reconciles provider truth |

Every case receives its own payment code, amount, operation ID, reviewed commit, and durable history. The timeline legend is generated from the event owners present in that case, and each marker uses the same color as its legend entry.

## Tests

Install the development dependencies and Chromium once:

~~~bash
uv sync
uv run playwright install chromium
~~~

Run the unit, integration, and Playwright suites:

~~~bash
./scripts/test.sh
~~~

Coverage includes agent contracts, model-required repair enforcement, guarded model-authored patches, SQLite projections, real Git repair and merge behavior, HTTP-to-Temporal handoff, Worker recovery reconciliation, provider-side key replay, payload conflict detection, deliberate duplicate creation, side-effect policy definitions, live runtime health, responsive timelines, provider evidence rendering, and both manual and automatic crash controls.

## Inspect local state

### SignalRoom projection

~~~bash
sqlite3 .signalroom/data/signalroom.db
~~~

~~~sql
SELECT id, status, updated_at
FROM agent_cases
ORDER BY created_at DESC;
~~~

~~~sql
SELECT
  id,
  json_extract(record, '$.evidence.commit.commit') AS reviewed_commit,
  json_extract(record, '$.outcome.success') AS outcome_reached
FROM agent_cases;
~~~

### Temporal history

Open [http://127.0.0.1:8233](http://127.0.0.1:8233), select the `default` namespace, and open a Workflow whose ID begins with `signalroom-case-`.

Temporal Event History is the authoritative recovery record. SQLite is intentionally a separate, readable UI projection.

### Demo provider state

~~~bash
sqlite3 .signalroom/data/third_party.db
~~~

~~~sql
SELECT id, payment_code, route, amount_cents, created_at
FROM payments
ORDER BY created_at DESC;

SELECT method, route, outcome, payment_id, idempotency_key
FROM request_audit
ORDER BY id DESC;
~~~

The provider database is deliberately separate from SignalRoom's projection database. This preserves the failure boundary where an external write can succeed before Temporal receives the Activity result.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/runtime` | Temporal, Worker, restart, chaos-demo, and Ollama health |
| `GET` | `/api/agent-definitions` | Active bounded agent definitions |
| `GET` | `/api/cases` | Case summaries |
| `GET` | `/api/cases/:id` | Full case, agents, evidence, and timeline projection |
| `GET` | `/api/reliability/provider` | Read-only payment rows and provider request audit for the selected case |
| `POST` | `/api/lab/run` | Start the unified repair Workflow; accepts `side_effect_strategy` and `auto_crash` |
| `POST` | `/api/demo/worker/crash` | Kill only the supervised Worker process |
| `POST` | `/api/cases/:id/approve-merge` | Signal approval for the exact reviewed commit |
| `POST` | `/api/cases/:id/reject-merge` | Signal rejection and close the case |

## Repository map

~~~text
signalroom/
├── backend/
│   ├── api/                     HTTP API and Temporal bridge
│   ├── agents/                  Definitions, model integration, and scoped tools
│   ├── orchestration/           Unified repair, side-effect, and verification Workflows
│   ├── reliability/             Backward-compatible policy harness and provider client
│   ├── runtime/                 Worker, supervisor, and process state
│   ├── storage/                 SQLite case projection
│   └── legacy/                  Generic event-to-outcome demonstration
├── frontend/                    Control room, styling, JavaScript, and essay
├── docs/                        Architecture diagram source
├── infra/                       Local Temporal Compose configuration
├── scripts/
│   ├── start.sh                Complete local-stack launcher and readiness checks
│   ├── dev.sh                  Backward-compatible alias for start.sh
│   └── test.sh                 Unit, integration, and browser test launcher
├── tests/                       Unit, integration, and Playwright suites
├── third_party_service/         Demo-only payment API and independent SQLite store
├── BLOG.md                      Companion essay
├── requirements.txt             Pinned runtime dependencies
└── pyproject.toml
~~~

The parent orchestrator and reusable child Agent Workflow are in [backend/orchestration/workflows.py](backend/orchestration/workflows.py). Activity implementations are in [backend/orchestration/activities.py](backend/orchestration/activities.py), and the model contract is in [backend/agents/harness.py](backend/agents/harness.py).

Generated runtime data lives under `.signalroom/` and is excluded from Git.

## Design boundaries

- Temporal coordinates deterministic control flow; it does not judge code correctness.
- Ollama authors the repair source, but its output is not accepted as evidence until guarded tools apply it and tests pass.
- Only allowlisted tools can produce evidence or mutate the repair lab.
- The Review Agent cannot edit code.
- Human approval names an immutable commit, not a vague recommendation.
- Any selected external-effect proof runs against that reviewed commit before the human merge gate.
- Merge and verification are separate stages.
- Post-merge outcome verification never repeats the live provider write.
- The synthetic per-case repository is isolated from the SignalRoom source repository.
- The Worker supervisor demonstrates process recovery; Temporal supplies Workflow recovery.
- Native idempotency is enforced by the provider, not by Temporal.
- A non-idempotent write with no reliable lookup is at-most-once and potentially uncertain; SignalRoom does not blindly retry it.

## Open-source stack

- SignalRoom: Apache License 2.0
- Temporal Server and Python SDK: MIT
- gpt-oss: Apache License 2.0
- Ollama: MIT
- SQLite: public domain

Read the companion essay: [Agents Are Becoming Less Dependent on Human Initiation—Not Human Accountability](BLOG.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
