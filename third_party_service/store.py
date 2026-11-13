"""SQLite persistence for the deliberately small third-party demo service.

This package is demo-only. It intentionally exposes one safe endpoint and one
unsafe endpoint so SignalRoom can prove what Temporal retries can and cannot do.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()


class IdempotencyConflict(ValueError):
    """The same key was reused for a different request payload."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PaymentStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY,
                    payment_code TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    route TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS payments_by_code
                    ON payments(payment_code);
                CREATE TABLE IF NOT EXISTS idempotency_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    method TEXT NOT NULL,
                    route TEXT NOT NULL,
                    payment_code TEXT,
                    idempotency_key TEXT,
                    outcome TEXT NOT NULL,
                    payment_id TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _payment(payload: dict[str, Any], route: str) -> dict[str, Any]:
        # Demo-only payload: no real payment provider or money movement exists.
        return {
            "id": f"pay_{uuid.uuid4().hex[:10]}",
            "payment_code": str(payload.get("payment_code") or "DEMO-PAYMENT"),
            "amount_cents": int(payload.get("amount_cents") or 4900),
            "route": route,
            "created_at": utc_now(),
        }

    @staticmethod
    def _insert_payment(db: sqlite3.Connection, payment: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?, ?)",
            (
                payment["id"], payment["payment_code"], payment["amount_cents"],
                payment["route"], payment["created_at"],
            ),
        )

    @staticmethod
    def _audit(
        db: sqlite3.Connection,
        method: str,
        route: str,
        payment_code: str | None,
        outcome: str,
        payment_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO request_audit(method, route, payment_code, idempotency_key, outcome, payment_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (method, route, payment_code, idempotency_key, outcome, payment_id, utc_now()),
        )

    def create_idempotent(self, payload: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], bool]:
        digest = payload_hash(payload)
        with _LOCK, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT request_hash, response_json FROM idempotency_requests WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    self._audit(
                        db, "POST", "/payments/idempotent", str(payload.get("payment_code")),
                        "key_payload_conflict", idempotency_key=idempotency_key,
                    )
                    raise IdempotencyConflict("Idempotency-Key was already used with a different payload")
                result = json.loads(existing["response_json"])
                self._audit(
                    db, "POST", "/payments/idempotent", result["payment_code"],
                    "replayed", result["id"], idempotency_key,
                )
                return result, True

            payment = self._payment(payload, "idempotent")
            self._insert_payment(db, payment)
            db.execute(
                "INSERT INTO idempotency_requests VALUES (?, ?, ?, ?)",
                (idempotency_key, digest, json.dumps(payment), utc_now()),
            )
            self._audit(
                db, "POST", "/payments/idempotent", payment["payment_code"],
                "created", payment["id"], idempotency_key,
            )
            return payment, False

    def create_non_idempotent(self, payload: dict[str, Any]) -> dict[str, Any]:
        with _LOCK, self.connect() as db:
            payment = self._payment(payload, "non_idempotent")
            self._insert_payment(db, payment)
            self._audit(
                db, "POST", "/payments/non-idempotent", payment["payment_code"],
                "created", payment["id"], None,
            )
            return payment

    def find(self, payment_code: str, audit: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM payments WHERE payment_code = ? ORDER BY created_at",
                (payment_code,),
            ).fetchall()
            if audit:
                self._audit(
                    db, "GET", "/payments", payment_code,
                    "found" if rows else "not_found",
                )
        return [dict(row) for row in rows]

    def snapshot(self, payment_code: str | None = None) -> dict[str, Any]:
        where = " WHERE payment_code = ?" if payment_code else ""
        params: tuple[Any, ...] = (payment_code,) if payment_code else ()
        with self.connect() as db:
            payments = db.execute(
                f"SELECT * FROM payments{where} ORDER BY created_at DESC", params,
            ).fetchall()
            audit = db.execute(
                f"SELECT * FROM request_audit{where} ORDER BY id DESC LIMIT 100", params,
            ).fetchall()
        return {
            "instance_id": "signalroom-demo-third-party",
            "demo_only": True,
            "database": str(self.path),
            "payment_code": payment_code,
            "payments": [dict(row) for row in payments],
            "audit": [dict(row) for row in audit],
        }

