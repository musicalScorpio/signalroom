#!/usr/bin/env python3
"""Run the local third-party payment simulator.

DEMO ONLY: this service never moves money. Its unsafe endpoint deliberately
creates duplicates so the reliability strategies are observable on a laptop.
"""

from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from third_party_service.store import IdempotencyConflict, PaymentStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / ".signalroom" / "data" / "third_party.db"


class DemoProviderHandler(BaseHTTPRequestHandler):
    store: PaymentStore

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[third-party {self.log_date_time_string()}] {format % args}")

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._json({"instance_id": "signalroom-demo-third-party", "status": "ok", "demo_only": True})
            return
        if parsed.path == "/payments":
            payment_code = params.get("payment_code", [""])[0]
            if not payment_code:
                self._json({"error": "payment_code is required"}, HTTPStatus.BAD_REQUEST)
                return
            payments = self.store.find(payment_code, audit=params.get("audit", ["0"])[0] == "1")
            self._json({"payment_code": payment_code, "payments": payments, "count": len(payments)})
            return
        if parsed.path == "/state":
            payment_code = params.get("payment_code", [None])[0]
            self._json(self.store.snapshot(payment_code))
            return
        self._json({"error": "Route not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._body()
        except (json.JSONDecodeError, ValueError):
            self._json({"error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/payments/idempotent":
            key = self.headers.get("Idempotency-Key", "").strip()
            if not key:
                self._json({"error": "Idempotency-Key header is required"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                payment, replayed = self.store.create_idempotent(payload, key)
            except IdempotencyConflict as error:
                self._json({"error": str(error)}, HTTPStatus.CONFLICT)
                return
            self._json(
                {"payment": payment, "replayed": replayed},
                HTTPStatus.OK if replayed else HTTPStatus.CREATED,
                {"Idempotency-Replayed": str(replayed).lower()},
            )
            return
        if path == "/payments/non-idempotent":
            payment = self.store.create_non_idempotent(payload)
            self._json({"payment": payment, "replayed": False}, HTTPStatus.CREATED)
            return
        self._json({"error": "Route not found"}, HTTPStatus.NOT_FOUND)


def make_server(host: str, port: int, db_path: Path = DEFAULT_DB) -> ThreadingHTTPServer:
    handler = type("ConfiguredDemoProviderHandler", (DemoProviderHandler,), {"store": PaymentStore(db_path)})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SignalRoom's demo-only third-party service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("THIRD_PARTY_PORT", "4180")))
    parser.add_argument("--db", type=Path, default=Path(os.getenv("THIRD_PARTY_DB", str(DEFAULT_DB))))
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.db)
    print(f"Demo third-party service listening on http://{args.host}:{args.port}")
    print(f"Demo-only database: {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

