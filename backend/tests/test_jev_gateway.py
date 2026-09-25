"""TDD tests for the Jev decision gateway (openspec add-jev-decision-gates group 1)."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.config import DEFAULT_JEV_CONFIG, load_jev_config
from backend.jev_client import (
    JevClient,
    JevQuestion,
    JevUnavailable,
    log_decision,
)


class _Handler(BaseHTTPRequestHandler):
    """Programmatic Jev endpoint: responds from the queue, records requests."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.server.requests.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "body": json.loads(self.rfile.read(length) or b"{}"),
            }
        )
        status, payload = self.server.responses.pop(0)
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        return


@pytest.fixture()
def jev_server(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.responses = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    yield server
    server.shutdown()
    server.server_close()


def _live_config(server, **overrides):
    config = {
        "jev": {
            "enabled": True,
            "mode": "live",
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "timeout_seconds": 2.0,
        }
    }
    config["jev"].update(overrides)
    return config


def test_default_config_disabled_and_off():
    config = load_jev_config(raw={})
    assert config["enabled"] is False
    assert config["mode"] == "off"
    assert config["model"] == DEFAULT_JEV_CONFIG["model"]
    assert all(not gate["enabled"] for gate in config["gates"].values())


def test_invalid_mode_falls_back_to_off():
    config = load_jev_config(raw={"jev": {"enabled": True, "mode": "bogus"}})
    assert config["mode"] == "off"


def test_threshold_clamping():
    config = load_jev_config(
        raw={
            "jev": {
                "timeout_seconds": 999,
                "gates": {
                    "routing": {"enabled": True, "min_confidence": 7},
                    "rag": {"enabled": True, "max_passages": 0, "min_relevance": -3},
                },
            }
        }
    )
    assert config["timeout_seconds"] <= 30
    assert config["gates"]["routing"]["min_confidence"] == 1.0
    assert config["gates"]["rag"]["max_passages"] >= 1
    assert config["gates"]["rag"]["min_relevance"] == 0.0


def test_client_none_when_disabled():
    assert JevClient.from_config(raw={}) is None
    assert JevClient.from_config(raw={"jev": {"enabled": True, "mode": "off"}}) is None


def test_mock_mode_deterministic_without_network():
    config = load_jev_config(
        raw={
            "jev": {
                "enabled": True,
                "mode": "mock",
                "mock_answers": {"worth": {"noul": 0.9}, "kind": {"choice": "execute", "confidence": 0.8}},
            }
        }
    )
    client = JevClient(config)
    questions = [
        JevQuestion("worth", "noul", "worth remembering"),
        JevQuestion("kind", "choice", "pick a kind", criteria={"execute": "do", "research": "look"}),
    ]
    first = client.ask("state text", questions)
    second = client.ask("state text", questions)
    assert first.answers["worth"].value == pytest.approx(0.9)
    assert first.answers["kind"].value == "execute"
    assert first.answers["kind"].confidence == pytest.approx(0.8)
    assert first == second


def test_live_request_shape_and_auth(jev_server):
    jev_server.responses.append(
        (
            200,
            {
                "model": "jev-1.13.0",
                "answers": {"worth": {"type": "noul", "noul": 0.93}},
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        )
    )
    client = JevClient(load_jev_config(raw=_live_config(jev_server)))
    response = client.ask(
        "some user message",
        [JevQuestion("worth", "noul", "Is this worth keeping?")],
    )
    assert response.answers["worth"].value == pytest.approx(0.93)
    assert response.model == "jev-1.13.0"
    request = jev_server.requests[0]
    assert request["path"] == "/v1/systemone"
    assert request["auth"] == "Bearer test-key-123"
    assert request["body"]["model"] == "jev-1.13.0"
    assert request["body"]["state"] == "some user message"
    assert request["body"]["questions"]["worth"]["type"] == "noul"
    assert request["body"]["questions"]["worth"]["instructions"] == "Is this worth keeping?"


def test_live_retries_once_on_server_error(jev_server):
    jev_server.responses.append((500, {"error": "boom"}))
    jev_server.responses.append(
        (200, {"model": "jev-1.13.0", "answers": {"ok": {"type": "noul", "noul": 0.5}}})
    )
    client = JevClient(load_jev_config(raw=_live_config(jev_server)))
    response = client.ask("state", [JevQuestion("ok", "noul", "ok?")])
    assert response.answers["ok"].value == pytest.approx(0.5)
    assert len(jev_server.requests) == 2


def test_live_unavailable_after_retry(jev_server):
    jev_server.responses.append((500, {"error": "boom"}))
    jev_server.responses.append((500, {"error": "boom"}))
    client = JevClient(load_jev_config(raw=_live_config(jev_server)))
    with pytest.raises(JevUnavailable):
        client.ask("state", [JevQuestion("ok", "noul", "ok?")])
    assert len(jev_server.requests) == 2


def test_live_unreachable_raises_unavailable(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123")
    config = load_jev_config(
        raw={
            "jev": {
                "enabled": True,
                "mode": "live",
                "base_url": "http://127.0.0.1:9",
                "timeout_seconds": 0.5,
            }
        }
    )
    client = JevClient(config)
    with pytest.raises(JevUnavailable):
        client.ask("state", [JevQuestion("ok", "noul", "ok?")])


def test_log_decision_contains_no_raw_state(caplog):
    with caplog.at_level(logging.INFO, logger="chat_agent.jev"):
        log_decision(
            "memory_write",
            state="secret user text about NIO budgets",
            questions=[JevQuestion("worth", "noul", "worth?")],
            response=None,
            adopted=False,
            elapsed=0.01,
            error="unavailable",
        )
    record = caplog.records[0]
    assert "jev_decision" in record.getMessage()
    assert "secret user text" not in record.getMessage()
    payload = json.loads(record.getMessage().split(" ", 1)[1])
    assert payload["access_point"] == "memory_write"
    assert payload["adopted"] is False
    assert payload["error"] == "unavailable"
    assert payload["state_hash"]
    assert payload["state_chars"] == len("secret user text about NIO budgets")
    assert payload["questions"][0]["name"] == "worth"
