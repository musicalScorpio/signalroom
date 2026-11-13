import os
import unittest

from playwright.sync_api import expect, sync_playwright


BASE_URL = os.getenv("SIGNALROOM_BASE_URL", "http://127.0.0.1:4173")


RECOVERY_CASE = {
    "id": "case_ui_recovery",
    "title": "Inventory contract drift breaks checkout",
    "status": "awaiting_merge_approval",
    "workflow_id": "signalroom-case-case_ui_recovery",
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-01T00:00:05+00:00",
    "event": {"source": "playwright", "summary": "Synthetic recovery UI test"},
    "context": {"side_effect_strategy": "native_idempotency"},
    "side_effect": {
        "strategy": "native_idempotency", "status": "provider_committed",
        "operation_id": "signalroom:case_ui_recovery:checkout-payment",
        "payment_code": "DEMO-SIDEFX_UI", "amount_cents": 4900,
        "activity_attempt": 1, "danger_window": True,
        "auto_crash": True, "auto_crash_state": "consumed",
    },
    "agents": {
        "investigator": {
            "name": "Reproduction Agent", "status": "completed", "turn": 3,
            "last_summary": "Required evidence collected.",
            "observations": [{"tool": "tests.reproduce", "result": {"exit_code": 1}}],
        },
        "change_intelligence": {
            "name": "Change Intelligence Agent", "status": "completed", "turn": 3,
            "last_summary": "Contract change identified.",
            "observations": [{"tool": "contract.inspect", "result": {"version": "v2"}}],
        },
        "repair": {
            "name": "Repair Agent", "status": "reasoning", "turn": 3,
            "last_summary": "Replacement Worker resumed the durable Activity.",
            "observations": [{"tool": "code.inspect", "result": {"editable_files": ["order_service.py"]}}],
        },
        "side_effect": {
            "name": "Side-Effect Execution Agent", "status": "waiting_for_receipt", "turn": 1,
            "last_summary": "Provider committed, but the create result is not durable yet.",
            "observations": [{"tool": "payments.create_idempotent", "result": {"payment": {"id": "pay_ui"}}}],
        },
    },
    "evidence": {},
    "approval": {"state": "pending"},
    "timeline": [
        {"id": "event-1", "kind": "event", "status": "complete", "at": "2026-01-01T00:00:00+00:00", "title": "Event captured", "detail": "", "data": None},
        {"id": "model-1", "kind": "model", "status": "complete", "at": "2026-01-01T00:00:00+00:00", "title": "Model reasoned", "detail": "", "data": None},
        {"id": "tool-1", "kind": "tool", "status": "complete", "at": "2026-01-01T00:00:00+00:00", "title": "Tool called", "detail": "", "data": None},
        {"id": "tool-result-1", "kind": "tool_result", "status": "complete", "at": "2026-01-01T00:00:00+00:00", "title": "Tool returned", "detail": "", "data": None},
        {"id": "handoff-1", "kind": "handoff", "status": "complete", "at": "2026-01-01T00:00:00+00:00", "title": "Agent handed off", "detail": "", "data": None},
        {"id": "human-1", "kind": "human", "status": "complete", "at": "2026-01-01T00:00:00+00:00", "title": "Human checkpoint", "detail": "", "data": None},
        {"id": "git-1", "kind": "git", "status": "complete", "at": "2026-01-01T00:00:00+00:00", "title": "Commit merged", "detail": "", "data": None},
        {"id": "outcome-1", "kind": "outcome", "status": "complete", "at": "2026-01-01T00:00:00+00:00", "title": "Outcome verified", "detail": "", "data": None},
        {"id": "provider-effect", "kind": "provider", "status": "danger", "at": "2026-01-01T00:00:00+00:00", "title": "Provider committed the external payment", "detail": "The provider committed, but Temporal has not recorded the result.", "data": {"payment": {"id": "pay_ui"}}},
        {
            "id": "worker-killed-111", "kind": "worker", "status": "interrupted",
            "at": "2026-01-01T00:00:01+00:00", "title": "Agent Worker killed",
            "detail": "Worker PID 111 was terminated.",
            "data": {
                "worker_pid": 111,
                "interrupted_agents": [
                    {"agent_id": "investigator", "name": "Reproduction Agent", "status": "reasoning", "turn": 2},
                    {"agent_id": "change_intelligence", "name": "Change Intelligence Agent", "status": "using_tool", "turn": 2},
                ],
            },
        },
        {
            "id": "worker-replaced-111", "kind": "worker", "status": "running",
            "at": "2026-01-01T00:00:03+00:00", "title": "Replacement Worker connected",
            "detail": "Supervisor started Worker PID 222.",
            "data": {
                "killed_worker_pid": 111,
                "replacement_worker_pid": 222,
                "restart_count": 1,
                "interrupted_agents": [
                    {"agent_id": "investigator", "name": "Reproduction Agent", "status": "reasoning", "turn": 2},
                    {"agent_id": "change_intelligence", "name": "Change Intelligence Agent", "status": "using_tool", "turn": 2},
                ],
            },
        },
        {
            "id": "temporal-replayed-111", "kind": "temporal", "status": "recovered",
            "at": "2026-01-01T00:00:04+00:00", "title": "Temporal replay restored Workflow context",
            "detail": "The replacement Worker resumed at awaiting_merge_approval.",
            "data": {
                "replayed_context": {"completed_agents": ["investigator", "change_intelligence"], "reviewed_commit": "abc123"},
                "recovered_workflow_state": {"status": "repairing"},
                "recovered_agent_states": {
                    "investigator": {
                        "agent_id": "investigator", "agent_name": "Reproduction Agent", "status": "reasoning", "turn": 2,
                        "assignment_context": {
                            "original_failure": {"error": "KeyError"},
                            "repository": "/tmp/repo",
                            "terminal_log": "\n".join(f"durable context line {index}" for index in range(100)),
                        },
                        "observations": [{"tool": "tests.reproduce", "result": {"exit_code": 1}}],
                    }
                },
            },
        },
        {
            "id": "signalroom-reattached-111", "kind": "signalroom", "status": "recovered",
            "at": "2026-01-01T00:00:05+00:00", "title": "SignalRoom reattached to the recovered case",
            "detail": "The projection still points to the same durable checkpoint.",
            "data": {"checkpoint_status": "awaiting_merge_approval"},
        },
        {
            "id": "worker-killed-222-cycle-2", "kind": "worker", "status": "interrupted",
            "at": "2026-01-01T00:00:06+00:00", "title": "Agent Worker killed",
            "detail": "Worker PID 222 was terminated.",
            "data": {
                "worker_pid": 222, "recovery_id": "2-ui", "recovery_cycle": 2,
                "interrupted_agents": [
                    {"agent_id": "repair", "name": "Repair Agent", "status": "reasoning", "turn": 3},
                ],
            },
        },
        {
            "id": "worker-replaced-222-cycle-2", "kind": "worker", "status": "running",
            "at": "2026-01-01T00:00:08+00:00", "title": "Replacement Worker connected",
            "detail": "Supervisor started Worker PID 333.",
            "data": {
                "killed_worker_pid": 222, "replacement_worker_pid": 333,
                "restart_count": 2, "recovery_id": "2-ui", "recovery_cycle": 2,
                "interrupted_agents": [
                    {"agent_id": "repair", "name": "Repair Agent", "status": "reasoning", "turn": 3},
                ],
            },
        },
    ],
}

RELIABILITY_RUN = {
    "id": "sidefx_ui",
    "title": "Provider Idempotency-Key replay",
    "strategy": "native_idempotency",
    "status": "danger_window",
    "workflow_id": "signalroom-side-effect-sidefx_ui",
    "operation_id": "signalroom:sidefx_ui:create-payment",
    "payment_code": "DEMO-SIDEFX_UI",
    "activity_attempt": 1,
    "danger_window": True,
    "agents": {
        "action_agent": {
            "id": "action_agent", "name": "Payment Action Agent",
            "purpose": "Executes create under an explicit side-effect policy.",
            "status": "waiting_for_receipt", "turn": 1, "max_turns": 4, "accent": "coral",
            "tools": ["payments.create_idempotent", "payments.create_once"],
            "last_summary": "Provider committed, but the create-tool result is not durable yet.",
            "observations": [{
                "id": "native-call-1", "tool": "payments.create_idempotent",
                "request": {"method": "POST", "route": "/payments/idempotent", "headers": {"Idempotency-Key": "signalroom:sidefx_ui:create-payment"}},
            }],
        },
        "recovery_agent": {
            "id": "recovery_agent", "name": "Outcome Recovery Agent",
            "purpose": "Uses provider evidence before another create.",
            "status": "queued", "turn": 0, "max_turns": 4, "accent": "blue",
            "tools": ["payments.lookup", "payments.verify_outcome"],
            "last_summary": "Waiting for a recovery decision.", "observations": [],
        },
    },
    "timeline": [
        {
            "id": "provider-effect", "kind": "provider", "status": "danger",
            "at": "2026-01-01T00:00:02+00:00", "title": "Provider committed the payment",
            "detail": "The Activity result is not in Temporal history yet.",
            "data": {"attempt": 1, "payment": {"id": "pay_ui"}},
        }
    ],
}

SECOND_RELIABILITY_RUN = {
    "id": "sidefx_ui_no_retry", "title": "Non-idempotent API: maximumAttempts=1",
    "strategy": "at_most_once", "status": "uncertain",
    "workflow_id": "signalroom-side-effect-sidefx_ui_no_retry",
    "operation_id": "signalroom:sidefx_ui_no_retry:create-payment",
    "payment_code": "DEMO-NO-RETRY-UI", "amount_cents": 5900,
    "activity_attempt": 1, "danger_window": False, "agents": {}, "timeline": [],
}

PROVIDER_STATE = {
    "instance_id": "signalroom-demo-third-party",
    "demo_only": True,
    "payment_code": "DEMO-SIDEFX_UI",
    "payments": [
        {"id": "pay_ui", "payment_code": "DEMO-SIDEFX_UI", "amount_cents": 4900, "route": "idempotent"}
    ],
    "audit": [
        {"method": "POST", "route": "/payments/idempotent", "outcome": "created", "payment_id": "pay_ui"}
    ],
}

SECOND_PROVIDER_STATE = {
    "instance_id": "signalroom-demo-third-party", "demo_only": True,
    "payment_code": "DEMO-NO-RETRY-UI",
    "payments": [{"id": "pay_no_retry", "payment_code": "DEMO-NO-RETRY-UI", "amount_cents": 5900, "route": "non_idempotent"}],
    "audit": [{"method": "POST", "route": "/payments/non-idempotent", "outcome": "created", "payment_id": "pay_no_retry"}],
}

AGENT_DEFINITIONS = {
    "investigator": {"name": "Reproduction Agent", "purpose": "Reproduce the failure.", "max_turns": 4, "accent": "coral", "tools": ["tests.reproduce"]},
    "change_intelligence": {"name": "Change Intelligence Agent", "purpose": "Inspect the contract change.", "max_turns": 4, "accent": "blue", "tools": ["contract.inspect"]},
    "repair": {"name": "Repair Agent", "purpose": "Author the bounded repair.", "max_turns": 10, "accent": "acid", "tools": ["code.inspect", "code.patch"]},
    "side_effect": {"name": "Side-Effect Execution Agent", "purpose": "Execute the selected durable API policy.", "max_turns": 4, "accent": "blue", "tools": ["payments.create_idempotent", "payments.create_once", "payments.lookup"]},
}


class RecoveryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def new_page(self):
        context = self.browser.new_context(reduced_motion="no-preference")
        self.addCleanup(context.close)
        return context.new_page()

    def install_recovery_routes(self, page):
        calls = {"provider": 0}
        runtime = {
            "temporal_connected": True,
            "chaos_demo_enabled": True,
            "worker": {"status": "online", "worker_pid": 222, "restart_count": 1},
            "ollama": {"connected": True, "model": "gpt-oss:20b", "model_available": True},
            "third_party": {"status": "ok", "demo_only": True},
        }

        def route_api(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/cases"):
                route.fulfill(json={"cases": [RECOVERY_CASE]})
            elif path.endswith("/api/cases/case_ui_recovery"):
                route.fulfill(json=RECOVERY_CASE)
            elif path.endswith("/api/runtime"):
                route.fulfill(json=runtime)
            elif path.endswith("/api/agent-definitions"):
                route.fulfill(json={"agents": AGENT_DEFINITIONS})
            elif path.endswith("/api/reliability/runs/sidefx_ui_no_retry"):
                route.fulfill(json=SECOND_RELIABILITY_RUN)
            elif path.endswith("/api/reliability/runs/sidefx_ui"):
                route.fulfill(json=RELIABILITY_RUN)
            elif path.endswith("/api/reliability/runs"):
                route.fulfill(json={"runs": [RELIABILITY_RUN, SECOND_RELIABILITY_RUN]})
            elif path.endswith("/api/reliability/provider"):
                calls["provider"] += 1
                route.fulfill(json=SECOND_PROVIDER_STATE if "DEMO-NO-RETRY-UI" in route.request.url else PROVIDER_STATE)
            else:
                route.abort()

        page.route("**/api/**", route_api)
        return calls

    def test_live_stack_reports_temporal_and_worker_online(self):
        page = self.new_page()
        self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")
        expect(page.locator("#trigger-button")).to_have_count(0)
        expect(page.locator("#hero-trigger")).to_have_text("Run the repair pipeline ↗")
        expect(page.locator("#temporal-status")).to_have_text("Connected")
        expect(page.locator("#worker-status")).to_have_text("Online")
        expect(page.locator("#runtime-indicator")).to_have_text("Temporal + Worker online")

    def test_timeline_payload_stays_open_across_live_polling(self):
        page = self.new_page()
        self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")

        details = page.locator('details[data-timeline-id="temporal-replayed-111"]')
        expect(details).to_be_visible()
        details.locator("summary").click()
        self.assertTrue(details.evaluate("element => element.open"))
        payload = details.locator("pre")
        payload.evaluate("element => { element.scrollTop = 140; element.scrollLeft = 9; }")
        page.locator("#recovery-proof").scroll_into_view_if_needed()
        page_y = page.evaluate("window.scrollY")
        page.wait_for_timeout(2200)
        self.assertTrue(details.evaluate("element => element.open"))
        expect(payload).to_contain_text('"reviewed_commit": "abc123"')
        self.assertGreater(payload.evaluate("element => element.scrollTop"), 100)
        self.assertAlmostEqual(page.evaluate("window.scrollY"), page_y, delta=3)

    def test_timeline_legend_matches_every_visible_marker(self):
        page = self.new_page()
        self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.locator('[data-timeline-filter="all"]').click()

        expected_kinds = ["signalroom", "temporal", "worker-killed", "worker-recovered", "event", "model", "tool", "tool_result", "handoff", "human", "git", "outcome", "provider"]
        legend_items = page.locator("#timeline-legend [data-kind]")
        expect(legend_items).to_have_count(len(expected_kinds))
        legend_kinds = legend_items.evaluate_all(
            "elements => elements.map(element => element.dataset.kind)"
        )
        self.assertEqual(legend_kinds, expected_kinds)
        for kind in expected_kinds:
            marker_selector = f'#timeline li.{kind}' if kind.startswith("worker-") else f'#timeline li[data-kind="{kind}"]'
            marker = page.locator(marker_selector).first.evaluate(
                "element => getComputedStyle(element, '::before').backgroundColor"
            )
            legend = page.locator(f'#timeline-legend [data-kind="{kind}"]').evaluate(
                "element => getComputedStyle(element, '::before').backgroundColor"
            )
            self.assertEqual(marker, legend, kind)

    def test_recovery_proof_animates_the_real_agent_context_and_syncs_agent_cards(self):
        page = self.new_page()
        self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")

        proof = page.locator("#recovery-proof")
        expect(proof).to_be_visible()
        expect(proof.locator(".recovery-node")).to_have_count(3)
        expect(proof.locator('[data-recovery-id="worker-killed-111"]')).to_have_text("Recovery 1")
        proof.locator('[data-recovery-id="worker-killed-111"]').click()
        expect(proof).to_contain_text("Reproduction Agent did not start from scratch")
        expect(proof).to_contain_text("tests.reproduce")
        expect(proof).to_contain_text("original failure")
        expect(proof).to_contain_text("Worker 222 picked up turn 2")
        expect(page.locator('.agent-card[data-agent-id="investigator"]')).to_have_class("agent-card recovery-focus")
        expect(page.locator('.agent-card[data-agent-id="change_intelligence"]')).to_have_class("agent-card recovery-focus")
        context_details = proof.locator("details[data-recovery-context]")
        context_details.locator("summary").click()
        self.assertTrue(context_details.evaluate("element => element.open"))
        page.wait_for_timeout(2200)
        self.assertTrue(context_details.evaluate("element => element.open"))
        expect(context_details.locator("pre")).to_contain_text('"assignment_context"')
        self.assertEqual(
            proof.locator(".recovery-link.broken").evaluate("element => getComputedStyle(element, '::after').animationName"),
            "fault-spark",
        )
        self.assertEqual(
            proof.locator(".recovery-link.restored").evaluate("element => getComputedStyle(element, '::after').animationName"),
            "context-flow",
        )

    def test_in_app_architecture_board_is_removed(self):
        page = self.new_page()
        self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")
        expect(page.locator(".architecture-section")).to_have_count(0)
        expect(page.get_by_text("Control flow vs. evidence", exact=True)).to_have_count(0)

    def test_evidence_payload_cannot_squash_case_timeline(self):
        page = self.new_page()
        page.set_viewport_size({"width": 1800, "height": 1000})
        self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")

        evidence = page.locator(".evidence-panel").bounding_box()
        timeline = page.locator(".timeline-panel").bounding_box()
        heading = page.locator(".timeline-panel h2").bounding_box()
        self.assertGreaterEqual(timeline["width"], 460)
        self.assertGreater(evidence["width"], timeline["width"])
        self.assertGreater(heading["width"], 150)

        page.set_viewport_size({"width": 1000, "height": 1000})
        page.wait_for_timeout(100)
        evidence = page.locator(".evidence-panel").bounding_box()
        timeline = page.locator(".timeline-panel").bounding_box()
        self.assertAlmostEqual(evidence["width"], timeline["width"], delta=2)

    def test_signalroom_mark_uses_moving_signal_animation(self):
        page = self.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded")
        bars = page.locator(".brand-mark i")
        self.assertEqual(bars.count(), 3)
        self.assertEqual(
            bars.evaluate_all("elements => elements.map(element => getComputedStyle(element).animationName)"),
            ["signal-bars", "signal-bars", "signal-bars"],
        )
        sweep = page.locator(".brand-mark").evaluate(
            "element => getComputedStyle(element, '::after').animationName"
        )
        self.assertEqual(sweep, "signal-sweep")

    def test_kill_worker_button_matches_theme_with_red_status_dot(self):
        page = self.new_page()
        self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")
        button = page.locator("#kill-worker-button")
        expect(button).to_be_enabled()
        styles = button.evaluate(
            "element => ({color: getComputedStyle(element).color, background: getComputedStyle(element).backgroundColor, dot: getComputedStyle(element.querySelector('.danger-dot')).backgroundColor})"
        )
        self.assertEqual(styles["color"], "rgb(23, 24, 21)")
        self.assertEqual(styles["background"], "rgb(215, 255, 85)")
        self.assertEqual(styles["dot"], "rgb(255, 59, 48)")

    def test_worker_lifecycle_stays_in_durable_recovery_with_correct_colors(self):
        page = self.new_page()
        self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")
        case_item = page.locator("#case-list .case-item")
        expect(case_item).not_to_contain_text("Worker process killed")
        expect(case_item).not_to_contain_text("Replacement Worker started")
        expect(page.locator("#case-list .case-runtime-log")).to_have_count(0)

        killed = page.locator("#timeline .worker-killed")
        recovered = page.locator("#timeline .worker-recovered")
        expect(killed).to_have_count(2)
        expect(recovered).to_have_count(2)
        colors = {
            "killed": killed.nth(0).evaluate("element => getComputedStyle(element, '::before').backgroundColor"),
            "recovered": recovered.nth(0).evaluate("element => getComputedStyle(element, '::before').backgroundColor"),
        }
        self.assertEqual(colors["killed"], "rgb(239, 73, 57)")
        self.assertEqual(colors["recovered"], "rgb(50, 200, 117)")

    def test_main_pipeline_exposes_four_strategies_and_case_bound_provider_data(self):
        page = self.new_page()
        calls = self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")

        strategy = page.locator("#repair-strategy")
        expect(strategy.locator("option")).to_have_count(4)
        expect(strategy.locator("option").nth(0)).to_have_text("Code repair only · no external API")
        expect(strategy.locator("option").nth(2)).to_have_text("Non-idempotent API · maximumAttempts=1")
        expect(strategy.locator("option").nth(3)).to_have_text("Non-idempotent API · 0 POST retries + GET recovery")
        expect(page.locator("#auto-crash")).to_be_enabled()
        page.locator("#auto-crash").check()
        expect(strategy).to_have_value("native_idempotency")
        strategy.select_option("native_idempotency")
        expect(page.locator("#auto-crash")).to_be_checked()
        expect(page.locator("#kill-worker-button")).to_be_enabled()
        expect(page.locator("#kill-worker-label")).to_have_text("Kill Worker — result gap")
        expect(page.locator("#reliability-run-status")).to_have_text("provider committed")
        expect(page.locator("#provider-data")).to_contain_text("pay_ui")
        expect(page.locator("#provider-data")).to_contain_text("Payments created")
        expect(page.locator("#reliability-timeline")).to_contain_text("Provider committed the external payment")
        expect(page.locator('.agent-card[data-agent-id="side_effect"]')).to_contain_text("Side-Effect Execution Agent")
        expect(page.locator("#external-call-flow .external-flow-node")).to_have_count(7)
        expect(page.locator("#provider-refresh")).to_contain_text("Live · refreshed")
        page.wait_for_timeout(1200)
        self.assertGreaterEqual(calls["provider"], 2)

    def test_case_context_and_long_api_logs_keep_their_carets_and_scroll_positions(self):
        page = self.new_page()
        self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")

        context = page.locator("details[data-case-context]")
        context.locator("summary").click()
        self.assertTrue(context.evaluate("element => element.open"))
        expect(context.locator("pre")).to_contain_text('"side_effect_strategy": "native_idempotency"')

        api_log = page.locator('#reliability-timeline details[data-reliability-timeline-id="provider-effect"]')
        api_log.locator("summary").click()
        self.assertTrue(api_log.evaluate("element => element.open"))
        api_log.locator("pre").evaluate("element => { element.scrollTop = 20; element.scrollLeft = 4; }")
        page.wait_for_timeout(2200)
        self.assertTrue(context.evaluate("element => element.open"))
        self.assertTrue(api_log.evaluate("element => element.open"))
        expect(api_log.locator("pre")).to_contain_text('"id": "pay_ui"')


if __name__ == "__main__":
    unittest.main()
