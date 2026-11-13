import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from backend.reliability import provider_client
from backend.reliability.workflows import AT_MOST_ONCE, NATIVE_WRITE_RETRY
from backend.storage.reliability_store import ReliabilityStore
from third_party_service.server import DemoProviderHandler
from third_party_service.store import IdempotencyConflict, PaymentStore


class ReliabilityLabTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_provider_idempotency_key_replays_original_payment(self):
        store = PaymentStore(self.root / "provider.db")
        payload = {"payment_code": "PAY-101", "amount_cents": 4900}
        first, first_replayed = store.create_idempotent(payload, "operation-101")
        second, second_replayed = store.create_idempotent(payload, "operation-101")

        self.assertFalse(first_replayed)
        self.assertTrue(second_replayed)
        self.assertEqual(first["id"], second["id"])
        snapshot = store.snapshot("PAY-101")
        self.assertEqual(len(snapshot["payments"]), 1)
        self.assertEqual([item["outcome"] for item in reversed(snapshot["audit"])], ["created", "replayed"])

    def test_provider_rejects_same_key_with_different_payload(self):
        store = PaymentStore(self.root / "provider.db")
        store.create_idempotent({"payment_code": "PAY-102", "amount_cents": 4900}, "operation-102")
        with self.assertRaisesRegex(IdempotencyConflict, "different payload"):
            store.create_idempotent({"payment_code": "PAY-102", "amount_cents": 9900}, "operation-102")
        self.assertEqual(len(store.snapshot("PAY-102")["payments"]), 1)

    def test_non_idempotent_endpoint_really_creates_duplicates(self):
        store = PaymentStore(self.root / "provider.db")
        payload = {"payment_code": "PAY-UNSAFE", "amount_cents": 4900}
        first = store.create_non_idempotent(payload)
        second = store.create_non_idempotent(payload)

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(store.find("PAY-UNSAFE")), 2)

    def test_reliability_projection_timeline_is_idempotent(self):
        store = ReliabilityStore(self.root / "signalroom.db")
        store.create({"id": "sidefx_test", "strategy": "validation", "status": "queued", "title": "Test", "timeline": []})
        store.timeline("sidefx_test", "temporal", "Scheduled", event_id="scheduled")
        store.timeline("sidefx_test", "temporal", "Scheduled", event_id="scheduled")
        record = store.patch("sidefx_test", {"status": "validating", "validation_check": 2})

        self.assertEqual(len(record["timeline"]), 1)
        self.assertEqual(record["validation_check"], 2)

    def test_temporal_write_policies_express_the_three_strategies(self):
        self.assertEqual(AT_MOST_ONCE.maximum_attempts, 1)
        self.assertGreater(NATIVE_WRITE_RETRY.maximum_attempts, 1)
        self.assertEqual(NATIVE_WRITE_RETRY.backoff_coefficient, 2.0)


class DemoProviderHttpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        store = PaymentStore(Path(self.tempdir.name) / "provider.db")
        handler = type("TestDemoProviderHandler", (DemoProviderHandler,), {"store": store})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tempdir.cleanup()

    def request(self, path, payload=None, headers=None):
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST" if payload is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)

    def test_http_routes_expose_native_and_non_idempotent_behavior(self):
        payload = {"payment_code": "HTTP-PAY", "amount_cents": 4900}
        first_status, first = self.request("/payments/idempotent", payload, {"Idempotency-Key": "http-operation"})
        second_status, second = self.request("/payments/idempotent", payload, {"Idempotency-Key": "http-operation"})
        self.request("/payments/non-idempotent", payload)
        _, lookup = self.request("/payments?payment_code=HTTP-PAY&audit=1")

        self.assertEqual(first_status, 201)
        self.assertEqual(second_status, 200)
        self.assertEqual(first["payment"]["id"], second["payment"]["id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(lookup["count"], 2)

    def test_signalroom_provider_client_uses_the_local_service(self):
        payload = {"payment_code": "CLIENT-PAY", "amount_cents": 4900}
        with patch.dict(os.environ, {"THIRD_PARTY_BASE_URL": self.base_url}):
            first = provider_client.create_idempotent(payload, "client-operation")
            second = provider_client.create_idempotent(payload, "client-operation")
            snapshot = provider_client.provider_snapshot("CLIENT-PAY")

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(snapshot["payments"]), 1)


if __name__ == "__main__":
    unittest.main()

