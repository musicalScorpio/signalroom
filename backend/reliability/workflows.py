"""Temporal Workflow comparing three external side-effect strategies."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


READ_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=4),
    maximum_attempts=4,
)
NATIVE_WRITE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=4),
    maximum_attempts=4,
)
AT_MOST_ONCE = RetryPolicy(maximum_attempts=1)


@workflow.defn
class SideEffectReliabilityWorkflow:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}

    async def _stage(
        self,
        run_id: str,
        status: str,
        title: str,
        detail: str,
        changes: dict[str, Any] | None = None,
        kind: str = "temporal",
    ) -> None:
        await workflow.execute_activity(
            "record_reliability_stage",
            {
                "run_id": run_id, "status": status, "title": title, "detail": detail,
                "changes": changes or {}, "data": changes or {}, "kind": kind,
                "event_id": f"stage-{run_id}-{status}",
            },
            start_to_close_timeout=timedelta(seconds=10), retry_policy=READ_RETRY,
        )
        self.state.update({"status": status, **(changes or {})})

    @workflow.run
    async def run(self, input: dict[str, Any]) -> dict[str, Any]:
        run_id = input["run_id"]
        strategy = input["strategy"]
        payment_code = input["payment_code"]
        operation_id = input["operation_id"]
        activity_input = {
            **input,
            "payload": {"payment_code": payment_code, "amount_cents": int(input.get("amount_cents", 4900))},
        }
        self.state = {
            "run_id": run_id, "status": "starting", "strategy": strategy,
            "operation_id": operation_id, "payment_code": payment_code,
        }
        await self._stage(
            run_id, "scheduled", "Agent execution policy recorded durably",
            "Temporal preserves the agent assignment, operation identity, and retry rule; the provider still owns the payment record.",
            {"strategy": strategy, "operation_id": operation_id},
        )

        if strategy == "native_idempotency":
            result = await workflow.execute_activity(
                "provider_idempotent_create", activity_input,
                start_to_close_timeout=timedelta(seconds=40),
                heartbeat_timeout=timedelta(seconds=2),
                retry_policy=NATIVE_WRITE_RETRY,
            )
            resolution = "provider_idempotency_replay" if result.get("replayed") else "first_attempt_completed"
            await self._stage(
                run_id, "completed", "Exactly one payment confirmed",
                "The restarted agent repeated POST with the stable Idempotency-Key; the provider replayed the first receipt instead of creating again.",
                {"result": result, "resolution": resolution, "duplicate_count": 0}, kind="outcome",
            )
            return self.state

        if strategy == "at_most_once":
            try:
                result = await workflow.execute_activity(
                    "provider_non_idempotent_create", activity_input,
                    start_to_close_timeout=timedelta(seconds=40),
                    heartbeat_timeout=timedelta(seconds=2),
                    retry_policy=AT_MOST_ONCE,
                )
            except ActivityError as error:
                await self._stage(
                    run_id, "uncertain", "Unsafe POST was not retried",
                    "The replacement agent was not allowed to call create again. That prevents a duplicate but requires provider evidence or human reconciliation.",
                    {"resolution": "manual_reconciliation_required", "failure": str(error), "duplicate_count": 0},
                    kind="outcome",
                )
                return self.state
            await self._stage(
                run_id, "completed", "Single unsafe attempt returned normally",
                "The provider response reached Temporal, so no recovery was required.",
                {"result": result, "resolution": "first_attempt_completed", "duplicate_count": 0}, kind="outcome",
            )
            return self.state

        if strategy != "validation":
            raise ValueError(f"Unknown side-effect strategy: {strategy}")

        preflight = await workflow.execute_activity(
            "provider_find_payment",
            {"run_id": run_id, "payment_code": payment_code, "check": 0},
            start_to_close_timeout=timedelta(seconds=8), retry_policy=READ_RETRY,
        )
        if preflight["count"] == 1:
            await self._stage(
                run_id, "completed", "Pre-existing payment adopted",
                "The safe preflight read found the business effect before any POST was sent.",
                {"result": preflight["payments"][0], "resolution": "preflight_validation", "duplicate_count": 0}, kind="outcome",
            )
            return self.state
        if preflight["count"] > 1:
            await self._stage(
                run_id, "failed", "Preflight found duplicate payments",
                "SignalRoom refused to create another payment and surfaced the data conflict.",
                {"result": preflight, "resolution": "duplicates_detected", "duplicate_count": preflight["count"] - 1}, kind="outcome",
            )
            return self.state

        try:
            result = await workflow.execute_activity(
                "provider_non_idempotent_create", activity_input,
                start_to_close_timeout=timedelta(seconds=40),
                heartbeat_timeout=timedelta(seconds=2),
                retry_policy=AT_MOST_ONCE,
            )
        except ActivityError:
            result = None

        if result:
            await self._stage(
                run_id, "completed", "Non-idempotent payment returned normally",
                "The one permitted POST completed and Temporal recorded its result.",
                {"result": result, "resolution": "first_attempt_completed", "duplicate_count": 0}, kind="outcome",
            )
            return self.state

        await self._stage(
            run_id, "validating", "Replacement agent chose validation, not another create",
            "The durable policy suppresses a second POST. The Outcome Recovery Agent may repeat only the safe GET after 1, 2, and 4 seconds.",
            {"backoff_seconds": [1, 2, 4]},
        )
        for check, delay in enumerate((1, 2, 4), start=1):
            await workflow.sleep(timedelta(seconds=delay))
            lookup = await workflow.execute_activity(
                "provider_find_payment",
                {"run_id": run_id, "payment_code": payment_code, "check": check},
                start_to_close_timeout=timedelta(seconds=8), retry_policy=READ_RETRY,
            )
            if lookup["count"] == 1:
                await self._stage(
                    run_id, "completed", "Payment recovered by validation",
                    "The read-only lookup found the effect created before the Worker died; no second POST was sent.",
                    {
                        "result": lookup["payments"][0], "resolution": "validation_recovered",
                        "validation_checks": check, "duplicate_count": 0,
                    }, kind="outcome",
                )
                return self.state
            if lookup["count"] > 1:
                await self._stage(
                    run_id, "failed", "Validation found duplicate payments",
                    "More than one external effect exists for the business reference.",
                    {"result": lookup, "resolution": "duplicates_detected", "duplicate_count": lookup["count"] - 1}, kind="outcome",
                )
                return self.state

        await self._stage(
            run_id, "uncertain", "Validation could not prove the outcome",
            "The provider returned no matching payment after exponential backoff. A human must decide whether another POST is safe.",
            {"resolution": "manual_reconciliation_required", "validation_checks": 3, "duplicate_count": 0}, kind="outcome",
        )
        return self.state

    @workflow.query
    def current_state(self) -> dict[str, Any]:
        return self.state

