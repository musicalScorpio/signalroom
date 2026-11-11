# SignalRoom

**Ambient agents, durable execution, accountable outcomes.**

SignalRoom is a fully local, open-source proof of concept for agents that begin with an event instead of a chat. It demonstrates a complete software-repair case—not a scripted animation:

~~~text
runtime failure → parallel investigation → repair branch → independent review
                → human merge approval → merge → outcome verification
~~~

The lab creates a real local Git repository where an Inventory v2 contract breaks checkout. Five bounded agents use recorded context and allowlisted tools to reproduce the failure, inspect the change, patch the code, run tests, commit a branch, review the exact diff, and verify the merged result. Temporal owns orchestration and recovery. SignalRoom’s SQLite database is a readable UI projection, not the Workflow recovery mechanism.

## The outcome

A successful case ends only when all of these facts exist:

- The original checkout failure was reproduced.
- Inventory v2 and the stale Order Service assumption were independently identified.
- A backward-compatible adapter was committed on an isolated branch.
- The full test suite passed.
- A separate Review Agent approved the exact diff.
- A human approved the exact commit SHA.
- The approved SHA was merged to main.
- A Verification Agent replayed checkout on main and measured success.

“Agent finished” is not the outcome. The verified checkout response and passing tests are the outcome.

## Tech stack

| Layer | Technology | Role in SignalRoom |
| --- | --- | --- |
| Operator UI | Vanilla HTML, CSS, and JavaScript | Live pipeline map, agent status, evidence, approval, recovery, and outcome views without a frontend build step |
| Local API | Python 3.11 standard-library HTTP server | Serves the UI and exposes case, runtime, crash-demo, and approval endpoints |
| Durable orchestration | Temporal Server and Temporal Python SDK 1.35 | Stores Workflow Event History, schedules tasks, runs child Workflows, and preserves durable waits |
| Agent runtime | Python agent harness with JSON command contracts | Loads versioned definitions, validates model responses, and restricts every agent to allowlisted tools |
| Local inference | Ollama with Qwen3, gpt-oss, or another JSON-capable open model | Produces structured agent tool and completion commands entirely on the local machine |
| Projection store | SQLite | Stores the operator-facing case, agent, evidence, and timeline projection |
| Repair environment | Git and Python unittest | Creates the isolated repository, branch, patch, test evidence, commit, review, merge, and verification |
| Process recovery | Python Worker supervisor | Restarts a killed Worker while Temporal retains the authoritative Workflow state |
| Runtime packaging | Docker or Colima, uv, and pip-compatible requirements | Runs Temporal locally and provides reproducible Python dependency installation |

## Code structure

The application code is grouped by responsibility:

~~~text
signalroom/
├── backend/
│   ├── api/                     HTTP API
│   ├── agents/                  Definitions, Ollama harness, and tools
│   ├── orchestration/           Temporal orchestrator, Activities, and client
│   ├── runtime/                 Worker, supervisor, and process state
│   ├── storage/                 SQLite projection store
│   └── legacy/                  Original generic event demonstration
├── frontend/                    HTML, CSS, JavaScript, and blog page
├── infra/                       Local Temporal Compose configuration
├── scripts/                     Development launcher
├── tests/                       Unit and integration tests
├── docs/                        Architecture source
├── .signalroom/                 Hidden generated DB, Worker state, and repair labs
├── pyproject.toml
├── requirements.txt
└── README.md
~~~

The main orchestrator is [backend/orchestration/workflows.py](backend/orchestration/workflows.py). It defines the parent IncidentCaseWorkflow and the reusable child AgentWorkflow. The actual agents are defined in [backend/agents/definitions.json](backend/agents/definitions.json); their model harness and tools live beside those definitions.

The repository root now contains only project-level documentation, dependency files, and top-level folders. Executable application code lives under backend, while browser code lives under frontend.

## Architecture

The diagram source lives in [docs/signalroom-architecture.mmd](docs/signalroom-architecture.mmd).

~~~mermaid
flowchart LR
    browser["SignalRoom UI"] -->|"POST /api/lab/run"| api["Local HTTP API"]
    api -->|"Start Workflow / send Signal"| client["Temporal gRPC client"]
    client --> temporal["Temporal service<br/>Event History + task queues"]

    supervisor["Worker supervisor"] -->|"spawn / respawn"| worker["Temporal Worker"]
    temporal -->|"Workflow + Activity tasks"| worker
    worker --> parent["IncidentCaseWorkflow"]
    parent -->|"parallel child Workflows"| agents["Investigator + Change Intelligence"]
    parent --> repair["Repair Agent"]
    parent --> review["Review Agent"]
    parent -->|"durable wait"| human["Human merge approval"]
    parent --> verify["Verification Agent"]

    agents --> activities["Typed Activities + allowlisted tools"]
    repair --> activities
    review --> activities
    verify --> activities
    activities --> harness["Structured agent harness"]
    harness -->|"local JSON turns"| ollama["Ollama model"]
    activities --> lab["Isolated Git repair lab<br/>code + tests + branch + commit"]
    activities --> sqlite["SQLite UI projection<br/>cases + evidence + timeline"]
    sqlite --> api
    human -->|"approve exact SHA"| api
~~~

### State ownership

| State | Owner | Purpose |
| --- | --- | --- |
| Workflow position, completed Activity results, pending approval signal | Temporal Event History | Reconstruct execution after a Worker dies |
| Case status, timeline, agent cards, evidence shown in the UI | SQLite agent_cases projection | Fast operator reads and local querying |
| Code, branch, commits, test output | Per-case repository under .signalroom/repair-lab/ | Real repair artifact and source of Git truth |
| Prompts, tools, evidence requirements, turn limits | backend/agents/definitions.json | Versionable agent definitions |
| Worker process lifecycle and restart count | backend/runtime/supervisor.py state file | Local crash/recovery demonstration |

Deleting SQLite would remove the UI projection, but it is not what lets Temporal resume a Workflow. Deleting the Temporal container destroys this development server’s Workflow history.

## Agent definitions

Agents are configured data, not hard-coded personas. Each definition has a purpose, system prompt, tool allowlist, required tools, and a maximum number of turns:

~~~json
{
  "repair": {
    "name": "Repair Agent",
    "purpose": "Create a minimal backward-compatible patch on an isolated branch.",
    "system_prompt": "Produce the smallest backward-compatible change, add regression coverage, run tests, and commit only inside the assigned worktree.",
    "tools": ["code.patch", "tests.run", "git.commit"],
    "required_tools": ["code.patch", "tests.run", "git.commit"],
    "max_turns": 6
  }
}
~~~

The model never receives an unrestricted shell. It selects from the definition’s tools; the harness validates every command before an Activity executes it. If Ollama is offline, the same evidence contract is completed by a deterministic safe policy and the fallback is recorded visibly.

## Quick start

Requirements:

- Python 3.11 or newer
- Docker or Colima
- [uv](https://github.com/astral-sh/uv)
- Optional: [Ollama](https://github.com/ollama/ollama) with a local open model

~~~bash
cd /Users/musicalscorpio/Documents/Codex/2026-11-09/this-time-i-an-tryign-to-2/outputs/signalroom
chmod +x scripts/dev.sh
./scripts/dev.sh
~~~

The project also includes [requirements.txt](requirements.txt) for a conventional pip environment:

~~~bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
~~~

The dependency versions in requirements.txt match uv.lock. The launcher still uses uv by default because it also starts and coordinates the local services.

Open:

- SignalRoom: [http://127.0.0.1:4173](http://127.0.0.1:4173)
- Temporal UI: [http://127.0.0.1:8233](http://127.0.0.1:8233)

The launcher starts or reuses the local Temporal container, syncs dependencies, starts the supervised Worker, and starts the API/UI server. It safely replaces an older SignalRoom process on port 4173 but refuses to kill an unrelated application.

### Use a local model

The default model name is qwen3:4b:

~~~bash
ollama pull qwen3:4b
OLLAMA_MODEL=qwen3:4b ./scripts/dev.sh
~~~

Any Ollama model that can return JSON can be selected. For example:

~~~bash
OLLAMA_MODEL=gpt-oss:20b ./scripts/dev.sh
~~~

The runtime rail shows whether the configured model is actually present. Every agent card records ollama/model-name or the explicit safe-fallback path.

## Run the demonstration

1. Click **Run the repair pipeline**.
2. Watch the Event-to-Outcome Map and two investigation agents begin in parallel.
3. At any non-terminal stage, click **Kill Agent Worker**.
4. The UI shows Restarting, then a new PID and an incremented restart count.
5. The replacement Worker replays Temporal history and resumes the unfinished Workflow task.
6. At the Human stage, inspect the real diff, test result, branch, and commit.
7. Click **Merge reviewed commit**.
8. Wait for the Verification Agent and the green **Outcome reached** result.

The supervisor resurrects the process; Temporal preserves the work. Those are deliberately shown as separate responsibilities.

## Query the local state

SignalRoom’s projection:

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
  json_extract(record, '$.commit.commit') AS reviewed_commit,
  json_extract(record, '$.outcome.success') AS outcome_reached
FROM agent_cases;
~~~

Temporal’s authoritative Workflow history is visible at [http://127.0.0.1:8233](http://127.0.0.1:8233) or through the Temporal CLI inside the container.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | /api/runtime | Temporal, Worker, restart, and Ollama status |
| GET | /api/agent-definitions | Runtime agent definitions |
| GET | /api/cases | Case projection list |
| GET | /api/cases/:id | Complete case, agent, evidence, and timeline projection |
| POST | /api/lab/run | Create the real repair lab and start its parent Workflow |
| POST | /api/demo/worker/crash | Kill only the supervised Worker |
| POST | /api/cases/:id/approve-merge | Signal approval of the exact reviewed commit |
| POST | /api/cases/:id/reject-merge | Signal rejection and close the case |

## Tests

~~~bash
uv run python -m unittest discover -s tests -p 'test_*.py' -v
~~~

The test suite includes an end-to-end local Git repair: expected initial failure, patch, passing tests, commit, independent diff review, merge, and verification on main.

## Important boundaries

- Temporal coordinates deterministic control flow; it does not decide whether code is correct.
- The harness and agent definitions decide which model/tool turn is allowed.
- Tool implementations perform the real tests, Git edits, commits, and verification.
- The human gate approves an immutable commit, not a vague recommendation.
- A model response is never treated as evidence unless a tool produced the underlying result.
- The lab repository is isolated and synthetic; it never edits the SignalRoom source repository.

## Open-source stack

- SignalRoom: Apache License 2.0
- Temporal Server and Python SDK: MIT
- Qwen3-4B: Apache License 2.0
- gpt-oss: Apache License 2.0
- Ollama: MIT
- SQLite: public domain

The companion essay is [BLOG.md](BLOG.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
