"""Sandboxed local Git lab used by the contract-drift repair agents."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from backend.legacy.core import ROOT, utc_now


LAB_ROOT = ROOT / ".signalroom" / "repair-lab"


INVENTORY_SOURCE = '''"""Local inventory service using the v2 stock contract."""

def get_inventory(sku: str) -> dict:
    return {
        "sku": sku,
        "contract_version": "2.0",
        "stock": {"on_hand": 15, "reserved": 3},
    }
'''

BROKEN_ORDER_SOURCE = '''"""Order service that still expects the inventory v1 contract."""

from inventory_service import get_inventory


def can_fulfill(sku: str, quantity: int) -> bool:
    inventory = get_inventory(sku)
    return inventory["available_quantity"] >= quantity


def checkout(sku: str, quantity: int) -> dict:
    if not can_fulfill(sku, quantity):
        return {"status": "rejected", "reason": "insufficient_stock"}
    return {"status": "accepted", "sku": sku, "quantity": quantity}
'''

TEST_SOURCE = '''import unittest
from unittest.mock import patch

import order_service


class ContractTests(unittest.TestCase):
    def test_checkout_accepts_inventory_v2_contract(self):
        self.assertEqual(order_service.checkout("sku-42", 2)["status"], "accepted")

    def test_checkout_rejects_quantity_above_available_stock(self):
        self.assertEqual(order_service.checkout("sku-42", 20)["status"], "rejected")

    def test_inventory_v1_remains_supported(self):
        with patch("order_service.get_inventory", return_value={"sku": "legacy", "available_quantity": 4}):
            self.assertTrue(order_service.can_fulfill("legacy", 4))


if __name__ == "__main__":
    unittest.main()
'''


def _run(repo: Path, args: list[str], timeout: int = 30) -> dict[str, Any]:
    process = subprocess.run(args, cwd=repo, text=True, capture_output=True, timeout=timeout)
    output = (process.stdout + process.stderr).strip()
    return {"command": args, "exit_code": process.returncode, "output": output[-12000:]}


def repo_for(case_id: str) -> Path:
    return LAB_ROOT / case_id / "repo"


def prepare_case(case_id: str) -> dict[str, Any]:
    repo = repo_for(case_id)
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "inventory_service.py").write_text(INVENTORY_SOURCE, encoding="utf-8")
    (repo / "order_service.py").write_text(BROKEN_ORDER_SOURCE, encoding="utf-8")
    (repo / "tests" / "test_contract.py").write_text(TEST_SOURCE, encoding="utf-8")
    (repo / "README.md").write_text("# Local checkout contract lab\n\nInventory v2 is live while Order Service still expects v1.\n", encoding="utf-8")
    if not (repo / ".git").exists():
        _run(repo, ["git", "init", "-b", "main"])
        _run(repo, ["git", "config", "user.name", "SignalRoom Agents"])
        _run(repo, ["git", "config", "user.email", "agents@signalroom.local"])
        _run(repo, ["git", "add", "."])
        _run(repo, ["git", "commit", "-m", "lab: inventory v2 contract drift"])
    failure = reproduce(case_id)
    log = {
        "timestamp": utc_now(), "level": "ERROR", "service": "order-service",
        "request": {"sku": "sku-42", "quantity": 2},
        "exception": "KeyError: available_quantity", "contract_version": "2.0",
    }
    (repo / "runtime.log").write_text(json.dumps(log, indent=2), encoding="utf-8")
    return {
        "repo": str(repo), "branch": "main", "base_commit": head(case_id),
        "event": {"type": "checkout.contract_failure", "source": "local-order-service", "summary": "Checkout fails after Inventory Service moved to contract v2", "request": {"sku": "sku-42", "quantity": 2}},
        "failure": failure,
    }


def head(case_id: str) -> str:
    result = _run(repo_for(case_id), ["git", "rev-parse", "HEAD"])
    return result["output"].splitlines()[-1] if result["exit_code"] == 0 else "unknown"


def reproduce(case_id: str) -> dict[str, Any]:
    return _run(repo_for(case_id), [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])


def logs(case_id: str) -> dict[str, Any]:
    path = repo_for(case_id) / "runtime.log"
    return {"path": str(path), "content": path.read_text(encoding="utf-8") if path.exists() else "Log not generated yet"}


def inspect_contract(case_id: str) -> dict[str, Any]:
    repo = repo_for(case_id)
    return {
        "inventory_service.py": (repo / "inventory_service.py").read_text(encoding="utf-8"),
        "order_service.py": (repo / "order_service.py").read_text(encoding="utf-8"),
        "finding": "Inventory returns stock.on_hand/reserved while Order Service reads available_quantity.",
    }


def inspect_code(case_id: str) -> dict[str, Any]:
    """Return the bounded repair surface that the model is allowed to change."""
    repo = repo_for(case_id)
    return {
        "editable_files": ["order_service.py"],
        "order_service.py": (repo / "order_service.py").read_text(encoding="utf-8"),
        "inventory_service.py": (repo / "inventory_service.py").read_text(encoding="utf-8"),
        "tests/test_contract.py": (repo / "tests" / "test_contract.py").read_text(encoding="utf-8"),
        "runtime.log": (repo / "runtime.log").read_text(encoding="utf-8"),
    }


def git_history(case_id: str) -> dict[str, Any]:
    return _run(repo_for(case_id), ["git", "log", "--oneline", "--decorate", "-5"])


def apply_repair(case_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate and apply source authored by the Repair Agent's Ollama model."""
    repo = repo_for(case_id)
    file_name = arguments.get("file")
    replacement = arguments.get("replacement_content")
    rationale = arguments.get("rationale", "")
    if file_name != "order_service.py":
        raise ValueError("Repair Agent may only replace order_service.py")
    if not isinstance(replacement, str) or not replacement.strip():
        raise ValueError("code.patch requires non-empty replacement_content authored by the model")
    if len(replacement.encode("utf-8")) > 50_000:
        raise ValueError("Model-authored replacement exceeds the 50 KB repair limit")
    compile(replacement, file_name, "exec")

    branch = f"signalroom/fix-contract-{case_id[-6:]}"
    existing = _run(repo, ["git", "branch", "--list", branch])["output"]
    if existing:
        _run(repo, ["git", "checkout", branch])
    else:
        _run(repo, ["git", "checkout", "-b", branch])
    (repo / file_name).write_text(replacement, encoding="utf-8")
    patch = _run(repo, ["git", "diff", "--", file_name])["output"]
    if not patch:
        raise ValueError("Model-authored replacement produced no source change")
    return {
        "success": True,
        "branch": branch,
        "file": file_name,
        "patch": patch,
        "rationale": rationale,
        "model_authored": True,
        "content_sha256": hashlib.sha256(replacement.encode("utf-8")).hexdigest(),
    }


def run_tests(case_id: str) -> dict[str, Any]:
    return reproduce(case_id)


def commit_repair(case_id: str) -> dict[str, Any]:
    repo = repo_for(case_id)
    _run(repo, ["git", "add", "order_service.py", "tests/test_contract.py"])
    if _run(repo, ["git", "diff", "--cached", "--quiet"])["exit_code"] != 0:
        _run(repo, ["git", "commit", "-m", "fix: support inventory v2 stock contract"])
    branch = _run(repo, ["git", "branch", "--show-current"])["output"]
    return {"branch": branch, "commit": head(case_id), "message": "fix: support inventory v2 stock contract"}


def review_diff(case_id: str) -> dict[str, Any]:
    repo = repo_for(case_id)
    diff = _run(repo, ["git", "diff", "main...HEAD", "--", "order_service.py", "tests/test_contract.py"])
    return {"approved": bool(diff["output"]), "scope": ["order_service.py"], "diff": diff["output"], "concerns": []}


def merge(case_id: str, expected_commit: str) -> dict[str, Any]:
    repo = repo_for(case_id)
    branch = _run(repo, ["git", "branch", "--show-current"])["output"]
    actual = head(case_id)
    if actual != expected_commit:
        raise ValueError(f"Approved commit changed: expected {expected_commit}, found {actual}")
    _run(repo, ["git", "checkout", "main"])
    if _run(repo, ["git", "merge-base", "--is-ancestor", expected_commit, "main"])["exit_code"] == 0:
        return {"merged": True, "already_merged": True, "commit": head(case_id), "source_branch": branch}
    result = _run(repo, ["git", "merge", "--no-ff", expected_commit, "-m", f"merge: SignalRoom repair {case_id}"])
    if result["exit_code"] != 0:
        raise RuntimeError(result["output"])
    return {"merged": True, "already_merged": False, "commit": head(case_id), "source_branch": branch}


def verify_main(case_id: str) -> dict[str, Any]:
    repo = repo_for(case_id)
    _run(repo, ["git", "checkout", "main"])
    tests = run_tests(case_id)
    try:
        sys.path.insert(0, str(repo))
        for module in ("order_service", "inventory_service"):
            sys.modules.pop(module, None)
        from order_service import checkout
        response = checkout("sku-42", 2)
    except Exception:
        response = {"status": "error", "traceback": traceback.format_exc()}
    finally:
        if str(repo) in sys.path:
            sys.path.remove(str(repo))
    return {
        "success": tests["exit_code"] == 0 and response.get("status") == "accepted",
        "tests": tests, "checkout_response": response, "main_commit": head(case_id),
    }
