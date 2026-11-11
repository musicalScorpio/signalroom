# SignalRoom

**The open-source control room for durable, event-driven ambient agents.**

SignalRoom turns a system signal into supervised, recoverable agent work. Instead of waiting for a person to open a chat, it detects that something changed, assembles evidence, routes bounded agents, pauses for a human decision when risk demands it, and verifies whether the intervention produced the intended outcome.

This repository is a fully local reference implementation built with Temporal, Ollama, Python, SQLite, and Git. It demonstrates a real software-repair workflow—not a scripted animation and not a chatbot wrapped around a model.

~~~text
event → context → policy → agent work → human exception → verified outcome → evidence
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
3. A Repair Agent creates a backward-compatible patch on an isolated Git branch.
4. An Independent Review Agent inspects the exact diff and reruns the tests.
5. Temporal waits durably for a human to approve the exact commit SHA.
6. An idempotent Activity merges only the reviewed commit.
7. An Outcome Verification Agent retests `main` against the original failure.

A case is successful only when the original checkout request succeeds on merged code and the regression suite passes. “The agent finished” is not accepted as a business outcome.

You can kill the Agent Worker at any point. The process loses its in-memory state, the supervisor starts a replacement, and Temporal reconstructs the Workflow from Event History. The case timeline shows the Worker death, retained durable checkpoint, replacement PID, replayed context, and resumed stage.

## Architecture

[View the standalone Mermaid source](docs/signalroom-architecture.mmd).

~~~mermaid
flowchart LR
    subgraph client ["Operator Surface"]
        browser["SignalRoom control room"]
    end

    subgraph gateway ["Local Interface"]
        http["HTTP and static web server"]
    end

    subgraph service ["Agent Execution Plane"]
        api["SignalRoom API and Temporal client"]
        supervisor["Worker supervisor"]
        worker["Temporal Worker: orchestrator, five bounded agents, and typed Activities"]
    end

    subgraph async ["Durable Control Plane"]
        temporal["Temporal Server: Workflow Event History, task queues, retries, and Signals"]
    end

    subgraph datastore ["Evidence and Artifacts"]
        sqlite["SQLite operator projection: cases, agents, evidence, and timeline"]
        gitlab["Per-case Git repair lab: code, tests, branch, commit, and merge"]
    end

    subgraph external ["Local Model Runtime"]
        ollama["Ollama with an open-weight model"]
    end

    browser -->|"HTTP actions and live polling"| http
    http -->|"Routes UI and API requests"| api
    api -.->|"gRPC: start, query, and Signal"| temporal
    temporal -.->|"Schedules Workflow and Activity tasks"| worker
    supervisor -->|"Spawns and respawns the process"| worker
    api -->|"Reads and writes the UI projection"| sqlite
    worker -->|"Records agents, evidence, and stages"| sqlite
    worker -->|"Runs scoped repair tools"| gitlab
    worker -.->|"Ollama: structured JSON turns"| ollama
~~~

### What each part owns

| Component | Owns | Does not own |
| --- | --- | --- |
| SignalRoom API | Event intake, case creation, operator endpoints, Temporal client calls | Workflow scheduling or agent reasoning |
| Temporal Server | Workflow Event History, task queues, retries, durable waits, Signals | Deciding whether a patch is correct |
| Temporal Worker | Workflow and Activity execution, child Agent Workflows, tool dispatch | Durable history after the process dies |
| Agent harness | Prompt construction, JSON command validation, tool allowlists, turn limits | Unrestricted shell access |
| Ollama model | Structured reasoning turns inside an agent contract | Orchestration, persistence, or authority to merge |
| SQLite | Fast operator-facing projection of cases, evidence, agents, and timeline | Workflow recovery |
| Per-case Git lab | Source-of-truth branch, commit, diff, tests, merge, and verification artifacts | Temporal control flow |
| Worker supervisor | Local process monitoring and respawn | Workflow replay |

### How Temporal actually helps

SignalRoom never writes directly to a queue. Its Temporal client starts or Signals a Workflow over gRPC. Temporal records that command in Workflow Event History and schedules Workflow or Activity tasks on the `signalroom-events` task queue.

Workers poll that queue. If the active Worker is killed:

1. Temporal still retains the Workflow history, completed Activity results, pending human wait, and Signal state.
2. The local supervisor starts a replacement Worker process.
3. The replacement polls the same task queue.
4. Temporal replays history to reconstruct deterministic Workflow state.
5. Completed Activities are not repeated; execution resumes at the unfinished durable step.

The supervisor resurrects the process. **Temporal resurrects the work.**

## Five bounded agents

Agents are versioned definitions in [backend/agents/definitions.json](backend/agents/definitions.json), not hard-coded personas.

| Agent | Responsibility | Allowed tools |
| --- | --- | --- |
| Reproduction Agent | Reproduce the failure and establish a factual signature | `tests.reproduce`, `logs.read` |
| Change Intelligence Agent | Identify contract drift and affected code paths | `contract.inspect`, `git.history` |
| Repair Agent | Create, test, and commit the smallest compatible fix | `code.patch`, `tests.run`, `git.commit` |
| Independent Review Agent | Review the exact commit and independently rerun tests | `review.diff`, `tests.run` |
| Outcome Verification Agent | Verify merged `main` against the original failure | `verify.main` |

Each definition includes:

- a purpose and system prompt;
- an explicit tool allowlist;
- evidence-producing tools that must run before completion;
- a maximum turn count;
- a version and UI identity.

The model never receives a general-purpose shell. It returns a structured JSON command, the harness validates it, and a typed Temporal Activity executes the corresponding scoped tool. Invalid, incomplete, or unavailable model output falls back to a deterministic safe policy, which is recorded in the evidence trail.

## Human authority and evidence

The human approval is not ceremonial. The UI presents:

- the reproduced failure;
- contract and Git history evidence;
- the isolated repair branch;
- the exact commit SHA and diff;
- independent review findings;
- passing test output.

Approval is sent to the waiting Workflow as a Temporal Signal. The merge Activity verifies that the approved SHA is still the reviewed SHA before changing `main`. A separate agent then measures the result.

This gives every run a reconstructable record:

~~~text
signal → source context → model command → tool evidence → handoff
       → approval Signal → exact merge → outcome verification
~~~

## Tech stack

| Layer | Technology |
| --- | --- |
| Control room | Vanilla HTML, CSS, and JavaScript |
| Local API | Python 3.11 standard-library HTTP server |
| Durable orchestration | Temporal Server and Temporal Python SDK |
| Local inference | Ollama with Qwen3, gpt-oss, or another JSON-capable open-weight model |
| Agent runtime | Python harness, versioned JSON definitions, typed tool contracts |
| Projection store | SQLite |
| Repair environment | Git and Python `unittest` |
| Local infrastructure | Docker or Colima, `uv`, and pip-compatible requirements |
| Browser testing | Playwright |

Everything runs locally. No hosted model or proprietary agent platform is required.

## Quick start

### Requirements

- Python 3.11 or newer
- Docker Desktop or Colima
- [uv](https://github.com/astral-sh/uv)
- Optional but recommended: [Ollama](https://github.com/ollama/ollama)

### Clone and run

~~~bash
git clone https://github.com/musicalScorpio/signalroom.git
cd signalroom

ollama pull qwen3:4b
OLLAMA_MODEL=qwen3:4b ./scripts/dev.sh
~~~

Or use another locally installed model:

~~~bash
OLLAMA_MODEL=gpt-oss:20b ./scripts/dev.sh
~~~

Open:

- SignalRoom: [http://127.0.0.1:4173](http://127.0.0.1:4173)
- Temporal Web UI: [http://127.0.0.1:8233](http://127.0.0.1:8233)

The launcher starts or reuses the Temporal development container, synchronizes Python dependencies, starts the supervised Worker, and serves the API and UI. Without Ollama, the same evidence contract runs through the visible deterministic fallback.

For a conventional pip environment:

~~~bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
~~~

## Run the crash-recovery demonstration

1. Select **Run the repair pipeline**.
2. Watch the Event-to-Outcome Map advance as the investigation agents work in parallel.
3. Select **Kill Agent Worker** during any non-terminal stage.
4. Watch the runtime rail change from Online to Restarting and back to Online with a new PID.
5. Expand the Worker, Temporal, and SignalRoom recovery entries in the Case Timeline.
6. At the Human stage, inspect the branch, exact commit, diff, review, and tests.
7. Select **Merge reviewed commit**.
8. Wait for the Outcome Verification Agent and the green **Outcome reached** state.

The timeline legend is generated from the event types present in the selected case, and every marker uses the same owner color as its legend entry.

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

Coverage includes agent contracts, replay-safe model output, SQLite projections, real Git repair and merge behavior, HTTP-to-Temporal handoff, Worker recovery reconciliation, live runtime health, durable payload disclosures, responsive timeline layout, owner-color legends, and the crash control.

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

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/runtime` | Temporal, Worker, restart, chaos-demo, and Ollama health |
| `GET` | `/api/agent-definitions` | Active bounded agent definitions |
| `GET` | `/api/cases` | Case summaries |
| `GET` | `/api/cases/:id` | Full case, agents, evidence, and timeline projection |
| `POST` | `/api/lab/run` | Prepare the repair lab and start the parent Workflow |
| `POST` | `/api/demo/worker/crash` | Kill only the supervised Worker process |
| `POST` | `/api/cases/:id/approve-merge` | Signal approval for the exact reviewed commit |
| `POST` | `/api/cases/:id/reject-merge` | Signal rejection and close the case |

## Repository map

~~~text
signalroom/
├── backend/
│   ├── api/                     HTTP API and Temporal bridge
│   ├── agents/                  Definitions, Ollama harness, and scoped tools
│   ├── orchestration/           Parent and child Workflows plus Activities
│   ├── runtime/                 Worker, supervisor, and process state
│   ├── storage/                 SQLite case projection
│   └── legacy/                  Generic event-to-outcome demonstration
├── frontend/                    Control room, styling, JavaScript, and essay
├── docs/                        Architecture diagram source
├── infra/                       Local Temporal Compose configuration
├── scripts/                     Development and test launchers
├── tests/                       Unit, integration, and Playwright suites
├── BLOG.md                      Companion essay
├── requirements.txt             Pinned runtime dependencies
└── pyproject.toml
~~~

The parent orchestrator and reusable child Agent Workflow are in [backend/orchestration/workflows.py](backend/orchestration/workflows.py). Activity implementations are in [backend/orchestration/activities.py](backend/orchestration/activities.py), and the model contract is in [backend/agents/harness.py](backend/agents/harness.py).

Generated runtime data lives under `.signalroom/` and is excluded from Git.

## Design boundaries

- Temporal coordinates deterministic control flow; it does not judge code correctness.
- Model output proposes a command; it is never treated as evidence by itself.
- Only allowlisted tools can produce evidence or mutate the repair lab.
- The Review Agent cannot edit code.
- Human approval names an immutable commit, not a vague recommendation.
- Merge and verification are separate stages.
- The synthetic per-case repository is isolated from the SignalRoom source repository.
- The Worker supervisor demonstrates process recovery; Temporal supplies Workflow recovery.

## Open-source stack

- SignalRoom: Apache License 2.0
- Temporal Server and Python SDK: MIT
- Qwen3: Apache License 2.0
- gpt-oss: Apache License 2.0
- Ollama: MIT
- SQLite: public domain

Read the companion essay: [Agents Are Becoming Less Dependent on Human Initiation—Not Human Accountability](BLOG.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
