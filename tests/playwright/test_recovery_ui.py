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
    "context": {},
    "agents": {},
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
        {
            "id": "worker-killed-111", "kind": "worker", "status": "interrupted",
            "at": "2026-01-01T00:00:01+00:00", "title": "Agent Worker killed",
            "detail": "Worker PID 111 was terminated.", "data": {"worker_pid": 111},
        },
        {
            "id": "temporal-replayed-111", "kind": "temporal", "status": "recovered",
            "at": "2026-01-01T00:00:04+00:00", "title": "Temporal replay restored Workflow context",
            "detail": "The replacement Worker resumed at awaiting_merge_approval.",
            "data": {"replayed_context": {"completed_agents": ["repair", "review"], "reviewed_commit": "abc123"}},
        },
        {
            "id": "signalroom-reattached-111", "kind": "signalroom", "status": "recovered",
            "at": "2026-01-01T00:00:05+00:00", "title": "SignalRoom reattached to the recovered case",
            "detail": "The projection still points to the same durable checkpoint.",
            "data": {"checkpoint_status": "awaiting_merge_approval"},
        },
    ],
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
        runtime = {
            "temporal_connected": True,
            "chaos_demo_enabled": True,
            "worker": {"status": "online", "worker_pid": 222, "restart_count": 1},
            "ollama": {"connected": True, "model": "gpt-oss:20b", "model_available": True},
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
                route.fulfill(json={"agents": {}})
            else:
                route.abort()

        page.route("**/api/**", route_api)

    def test_live_stack_reports_temporal_and_worker_online(self):
        page = self.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded")
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
        page.wait_for_timeout(2200)
        self.assertTrue(details.evaluate("element => element.open"))
        expect(details.locator("pre")).to_contain_text('"reviewed_commit": "abc123"')

    def test_timeline_legend_matches_every_visible_marker(self):
        page = self.new_page()
        self.install_recovery_routes(page)
        page.goto(BASE_URL, wait_until="domcontentloaded")

        expected_kinds = ["signalroom", "temporal", "worker", "event", "model", "tool", "tool_result", "handoff", "human", "git", "outcome"]
        legend_items = page.locator("#timeline-legend [data-kind]")
        expect(legend_items).to_have_count(len(expected_kinds))
        legend_kinds = legend_items.evaluate_all(
            "elements => elements.map(element => element.dataset.kind)"
        )
        self.assertEqual(legend_kinds, expected_kinds)
        for kind in expected_kinds:
            marker = page.locator(f'#timeline li[data-kind="{kind}"]').first.evaluate(
                "element => getComputedStyle(element, '::before').backgroundColor"
            )
            legend = page.locator(f'#timeline-legend [data-kind="{kind}"]').evaluate(
                "element => getComputedStyle(element, '::before').backgroundColor"
            )
            self.assertEqual(marker, legend, kind)

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


if __name__ == "__main__":
    unittest.main()
