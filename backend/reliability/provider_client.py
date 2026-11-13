"""Small stdlib HTTP client for SignalRoom's local demo provider."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def provider_base_url() -> str:
    return os.getenv("THIRD_PARTY_BASE_URL", "http://127.0.0.1:4180").rstrip("/")


def _request(path: str, method: str = "GET", payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{provider_base_url()}{path}",
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.load(error)
        except Exception:
            detail = {"error": str(error)}
        raise RuntimeError(detail.get("error", str(error))) from error


def health() -> dict[str, Any]:
    try:
        return _request("/health")
    except Exception as error:
        return {"instance_id": "signalroom-demo-third-party", "status": "offline", "error": str(error), "demo_only": True}


def create_idempotent(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return _request("/payments/idempotent", "POST", payload, {"Idempotency-Key": key})


def create_non_idempotent(payload: dict[str, Any]) -> dict[str, Any]:
    return _request("/payments/non-idempotent", "POST", payload)


def find_payments(payment_code: str, audit: bool = False) -> dict[str, Any]:
    query = urllib.parse.urlencode({"payment_code": payment_code, "audit": "1" if audit else "0"})
    return _request(f"/payments?{query}")


def provider_snapshot(payment_code: str | None = None) -> dict[str, Any]:
    query = f"?{urllib.parse.urlencode({'payment_code': payment_code})}" if payment_code else ""
    try:
        return _request(f"/state{query}")
    except Exception as error:
        return {
            "instance_id": "signalroom-demo-third-party", "demo_only": True,
            "status": "offline", "error": str(error), "payments": [], "audit": [],
        }

