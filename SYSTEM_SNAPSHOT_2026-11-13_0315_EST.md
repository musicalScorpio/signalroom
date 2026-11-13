# SignalRoom system snapshot

Captured: **November 13, 2026 at 03:15–03:18 EST**  
Capture mode: **read-only** — no service, Worker, Workflow, or database record was restarted or modified during capture.

## Conclusion

The latest repair pipeline is **not running and not waiting**. It is terminally failed in Temporal.

SignalRoom looks stuck because its SQLite projection was not updated when the parent Workflow failed:

| Source of truth | Recorded state |
| --- | --- |
| Temporal Workflow history | `FAILED` |
| SignalRoom `agent_cases` projection | `repairing` |
| Repair Agent projection | `reasoning`, turn 2 |
| Local Worker process | `online` |

The Worker and supporting services are alive. Restarting the Worker alone cannot resume this Workflow because Temporal has already closed it as failed.

## Affected execution

```text
Case ID:       case_a1576462
Workflow ID:   signalroom-case-case_a1576462
Workflow type: IncidentCaseWorkflow
Run ID:        01a1d3f1-7e4b-7f01-81da-df652ca311f5
Task queue:    signalroom-events
Started:       2026-11-13 08:06:21 UTC / 03:06:21 EST
Failed:        2026-11-13 08:10:24 UTC / 03:10:24 EST
Runtime:       4m 2.49s
History:       47 events, 93,931 bytes
```

## Runtime health at capture

```text
SignalRoom API:       online, PID 57627, 127.0.0.1:4173
Third-party service:  online, PID 57621, 127.0.0.1:4180
Temporal container:   signalroom-temporal, Up 2 days
Temporal ports:       7233 and 8233
Temporal connection:  connected
Worker supervisor:    online, PID 57626
Worker:               online, PID 60110
Ollama:               connected
Ollama model:         gpt-oss:20b, loaded
```

Worker supervisor state:

```json
{
  "status": "online",
  "supervisor_pid": 57626,
  "worker_pid": 60110,
  "restart_count": 5,
  "started_at": "2026-11-13T07:56:51+00:00",
  "last_exit_at": "2026-11-13T08:06:29+00:00",
  "last_restart_at": "2026-11-13T08:06:32+00:00",
  "last_exit_code": -9
}
```

## What happened

1. SignalRoom created `case_a1576462` and Temporal started the parent Workflow at `03:06:21 EST`.
2. The original Worker, PID `59425`, was deliberately killed at `03:06:29 EST`.
3. The supervisor started replacement Worker PID `60110` at `03:06:32 EST`.
4. Temporal preserved the Workflow history and replayed it on the replacement Worker.
5. The original in-flight investigation model Activities did not resume immediately; their old execution timeout delayed retry until approximately `03:09:22 EST`.
6. Both investigation child Workflows then completed.
7. The parent entered `repairing` and started child Workflow `signalroom-case_a1576462-repair` at `03:10:08 EST`.
8. Repair Agent turn 1 completed `code.inspect`.
9. Repair Agent turn 2 scheduled `agent_model_turn` with a four-second heartbeat timeout.
10. Temporal started three Activity attempts. Each attempt hit `TIMEOUT_TYPE_HEARTBEAT` before the local Ollama repair completed.
11. Temporal closed the Repair Agent child Workflow and then the parent Workflow as `FAILED` at `03:10:24 EST`.
12. No failure handler synchronized that terminal state back into SignalRoom's SQLite projection, leaving the UI at `repairing`.

## Temporal failure evidence

Parent Workflow result:

```text
Status: FAILED
Message: Child Workflow execution failed
Child Workflow: signalroom-case_a1576462-repair
Activity: agent_model_turn
Retry state: RETRY_STATE_MAXIMUM_ATTEMPTS_REACHED
Cause: activity Heartbeat timeout
Timeout type: TIMEOUT_TYPE_HEARTBEAT
```

Repair Activity timing:

```text
Activity scheduled: 08:10:08.969 UTC
Attempt 3 started:   08:10:19.987 UTC
Final timeout:       08:10:23.993 UTC
Heartbeat timeout:   4 seconds
Maximum attempts:    3
```

## SignalRoom projection evidence

SQLite still records:

```text
case status:          repairing
case updated_at:      2026-11-13T08:10:19+00:00
repair agent status:  reasoning
repair agent turn:    2
timeline entries:     42
```

The last projected Repair Agent summary is:

```text
Load the bounded source, tests, dependency, and failure log before asking Ollama to repair code.
```

This is stale. Temporal's Event History is authoritative and says the execution has failed.

## Root causes

There are two separate defects:

1. **Heartbeat configuration is too aggressive for the local model path.** A four-second timeout caused three fast retries and failure while `gpt-oss:20b` was generating locally. The heartbeat implementation or timeout needs to be validated under actual Ollama latency; four seconds is not currently safe.
2. **Terminal Workflow failures are not projected to SignalRoom.** An unhandled child Workflow failure closes the Temporal parent, but no Activity records `failed` in `agent_cases`. The UI therefore presents a terminal failure as live work.

There was also an earlier recovery delay: the killed Worker's in-flight Activities were not detected until their timeout, which made recovery appear inactive for roughly 173 seconds.

## Preservation note

The failed Workflow history, SQLite case projection, repair-lab repository, and Worker restart evidence were left intact for debugging. No reset, replay, deletion, or new repair run was performed as part of this snapshot.
