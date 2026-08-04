#!/usr/bin/env python3
"""Live Ella memory continuity smoke test.

This is intentionally a live smoke harness, not a unit test. It validates the
critical deployed path:

MCP proposal -> Observer safe applier -> canonical observer_memory ->
MCP/canonical recall.

It can also seed synthetic multi-channel canonical events to verify that the
shared timeline still retrieves iMessage/app chat/voice events in one surface.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

DEFAULT_PLATO_UID = "5aGC5YE9BnhcSoTxxtT4ar6ILQy2"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_request(method: str, url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
    started = time.monotonic()
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        data = response.read()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return json.loads(data or b"{}"), elapsed_ms


def _get_json(url: str, headers: dict[str, str] | None = None):
    started = time.monotonic()
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as response:
        data = response.read()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return json.loads(data or b"{}"), elapsed_ms


class SmokeFailure(Exception):
    pass


class MemorySmoke:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.backend_url = args.backend_url.rstrip("/")
        self.mcp_url = self.backend_url + "/v1/ella/plato/mcp"
        self.metrics: dict[str, int] = {}
        self.artifacts: dict[str, Any] = {}

    def enforce_profile_safety(self) -> None:
        writes_real_plato = self.args.uid == DEFAULT_PLATO_UID or self.args.canonical_identity.lower() == "plato"
        if writes_real_plato and not self.args.allow_real_profile:
            raise SmokeFailure(
                "Refusing to write live memory smoke data to Plato without --allow-real-profile. "
                "Use an ephemeral test uid/profile for routine regression runs."
            )

    def _mcp_call(self, method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
        if not self.args.mcp_token:
            raise SmokeFailure("MCP token is required for MCP smoke")
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        response, elapsed = _json_request(
            "POST",
            self.mcp_url,
            payload,
            headers={"Authorization": f"Bearer {self.args.mcp_token}"},
        )
        self.metrics[f"mcp_{method.replace('/', '_')}_ms"] = elapsed
        if "error" in response:
            raise SmokeFailure(f"MCP {method} failed: {response['error']}")
        return response

    def _observer_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.args.observer_token:
            raise SmokeFailure("Observer token is required for apply smoke")
        response, elapsed = _json_request(
            "POST",
            self.backend_url + path,
            payload,
            headers={"X-Ella-Observer-Token": self.args.observer_token},
        )
        self.metrics[path.strip("/").replace("/", "_") + "_ms"] = elapsed
        return response

    def check_health(self) -> None:
        health, elapsed = _get_json(self.backend_url + "/v1/health")
        self.metrics["backend_health_ms"] = elapsed
        if health.get("status") != "ok":
            raise SmokeFailure(f"backend health failed: {health}")
        observer, elapsed = _get_json(
            self.backend_url + "/v1/ella/observer/health",
            headers={"X-Ella-Observer-Token": self.args.observer_token},
        )
        self.metrics["observer_health_ms"] = elapsed
        if observer.get("ok") is not True:
            raise SmokeFailure(f"observer health failed: {observer}")

    def check_mcp_tools(self) -> None:
        tools_response = self._mcp_call("tools/list", {}, 10)
        tools = [tool.get("name") for tool in tools_response.get("result", {}).get("tools", [])]
        self.artifacts["mcp_tools"] = tools
        required = {"companion_propose_change", "companion_get_proposal_status", "plato_search_memory"}
        missing = sorted(required - set(tools))
        if missing:
            raise SmokeFailure(f"MCP tools missing: {missing}")

    def create_memory_proposal(self) -> str:
        phrase = self.args.phrase
        result = self._mcp_call(
            "tools/call",
            {
                "name": "companion_propose_change",
                "arguments": {
                    "proposal_type": "memory_note",
                    "title": "E2E memory smoke test",
                    "description": f"Remember the E2E memory smoke phrase: {phrase}.",
                    "requested_change": {"memory": f"The E2E memory smoke phrase is {phrase}."},
                    "target": {"canonical_identity": self.args.canonical_identity},
                    "evidence": [{"kind": "automated_e2e_smoke", "synthetic": True, "at": _now_iso()}],
                    "confidence": 0.96,
                    "idempotency_key": f"ella-memory-e2e:{phrase}",
                },
            },
            20,
        )
        text = result["result"]["content"][0]["text"]
        payload = json.loads(text)
        proposal = payload.get("proposal") or {}
        proposal_id = proposal.get("proposal_id")
        if not proposal_id:
            raise SmokeFailure(f"MCP proposal did not return proposal_id: {payload}")
        self.artifacts["proposal_id"] = proposal_id
        self.artifacts["proposal_created"] = payload.get("created")
        return proposal_id

    def apply_memory_proposal(self) -> dict[str, Any]:
        result = self._observer_post(
            "/v1/ella/observer/apply-pending",
            {
                "uid": self.args.uid,
                "dry_run": False,
                "limit": 50,
                "min_confidence": 0.9,
                "proposal_types": ["memory_note", "profile_update"],
            },
        )
        self.artifacts["apply_result"] = {
            "proposal_count": result.get("proposal_count"),
            "applied_count": result.get("applied_count"),
            "error_count": result.get("error_count"),
            "applied_event_ids": result.get("applied_event_ids"),
        }
        if result.get("error_count"):
            raise SmokeFailure(f"apply-pending had errors: {result}")
        return result

    def check_proposal_status(self, proposal_id: str) -> None:
        result = self._mcp_call(
            "tools/call",
            {"name": "companion_get_proposal_status", "arguments": {"proposal_id": proposal_id}},
            30,
        )
        payload = json.loads(result["result"]["content"][0]["text"])
        self.artifacts["proposal_status"] = payload.get("status")
        if payload.get("status") != "applied":
            raise SmokeFailure(f"proposal status is not applied: {payload}")

    def search_memory(self, query: str, channels: list[str]) -> list[dict[str, Any]]:
        result = self._mcp_call(
            "tools/call",
            {
                "name": "plato_search_memory",
                "arguments": {"query": query, "channels": channels, "max_results": 8},
            },
            40,
        )
        payload = json.loads(result["result"]["content"][0]["text"])
        results = payload.get("results") or []
        self.artifacts[f"search_{'_'.join(channels) or 'all'}"] = {
            "source": payload.get("source"),
            "result_count": len(results),
            "first_channels": [item.get("channel") for item in results[:5]],
        }
        return results

    def check_memory_recall(self) -> None:
        results = self.search_memory(self.args.phrase, ["observer_memory"])
        if not any(self.args.phrase.lower() in str(item.get("text") or "").lower() for item in results):
            raise SmokeFailure("observer_memory search did not find the applied MCP phrase")

    def seed_multichannel_events(self) -> None:
        if not self.args.seed_multichannel:
            return
        phrase = self.args.phrase
        batch = []
        for channel in ("ios_chat", "imessage", "ios_voice"):
            batch.append(
                {
                    "uid": self.args.uid,
                    "canonical_identity": self.args.canonical_identity,
                    "event_id": f"e2e:{channel}:{uuid.uuid4()}",
                    "channel": channel,
                    "provider": "ella-memory-e2e",
                    "role": "user",
                    "text": f"E2E multichannel memory continuity phrase for {channel}: {phrase}.",
                    "started_at": _now_iso(),
                    "source_ref": {"source_id": f"ella-memory-e2e:{channel}:{phrase}"},
                    "metadata": {"test": "ella_memory_e2e_smoke", "synthetic": True, "phrase": phrase},
                }
            )
        ledger_token = _env("ELLA_EVENT_LEDGER_TOKEN")
        if not ledger_token:
            raise SmokeFailure("ELLA_EVENT_LEDGER_TOKEN is required for canonical event ingestion")
        response, elapsed = _json_request(
            "POST",
            self.backend_url + "/v1/ella/events",
            {"events": batch},
            {"X-Ella-Event-Ledger-Key": ledger_token},
        )
        self.metrics["write_multichannel_events_ms"] = elapsed
        self.artifacts["multichannel_write"] = response
        if response.get("ok") is not True:
            raise SmokeFailure(f"multi-channel event write failed: {response}")
        results = self.search_memory(phrase, ["ios_chat", "imessage", "ios_voice"])
        found_channels = {
            item.get("channel") for item in results if phrase.lower() in str(item.get("text") or "").lower()
        }
        missing = {"ios_chat", "imessage", "ios_voice"} - found_channels
        if missing:
            raise SmokeFailure(f"multi-channel search missed channels: {sorted(missing)}")

    def enforce_latency(self) -> None:
        slow = {
            key: value
            for key, value in self.metrics.items()
            if value > self.args.max_latency_ms and not key.startswith("mcp_tools_call")
        }
        self.artifacts["latency_threshold_ms"] = self.args.max_latency_ms
        if slow:
            raise SmokeFailure(f"latency threshold exceeded: {slow}")

    def run(self) -> dict[str, Any]:
        self.enforce_profile_safety()
        self.check_health()
        self.check_mcp_tools()
        proposal_id = self.create_memory_proposal()
        self.apply_memory_proposal()
        self.check_proposal_status(proposal_id)
        self.check_memory_recall()
        self.seed_multichannel_events()
        if self.args.enforce_latency:
            self.enforce_latency()
        return {
            "ok": True,
            "uid": self.args.uid,
            "phrase": self.args.phrase,
            "metrics_ms": self.metrics,
            "artifacts": self.artifacts,
        }


def _default_phrase() -> str:
    return "e2e-" + uuid.uuid4().hex[:10]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live Ella memory continuity smoke")
    parser.add_argument("--backend-url", default=_env("ELLA_BACKEND_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--uid", default=_env("ELLA_PLATO_UID", DEFAULT_PLATO_UID))
    parser.add_argument("--canonical-identity", default="plato")
    parser.add_argument("--mcp-token", default=_env("ELLA_PLATO_MCP_TOKEN", "").split(",")[0])
    parser.add_argument("--observer-token", default=_env("ELLA_OBSERVER_ADMIN_TOKEN", _env("ELLA_ADMIN_TOKEN", "")))
    parser.add_argument("--phrase", default=_default_phrase())
    parser.add_argument("--seed-multichannel", action="store_true")
    parser.add_argument("--enforce-latency", action="store_true")
    parser.add_argument("--max-latency-ms", type=int, default=5000)
    parser.add_argument(
        "--allow-real-profile",
        action="store_true",
        help="Allow writes to the real Plato profile. Required because this smoke creates durable proposals/events.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        result = MemorySmoke(args).run()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "phrase": args.phrase}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
