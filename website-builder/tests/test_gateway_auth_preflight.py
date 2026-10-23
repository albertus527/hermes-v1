"""Gateway authentication preflight: real temp profiles, real HTTP semantics.

No live inference, no operator homes, no paid calls. Every probe is a
non-billable ``GET /models``; the tests drive it with a local stub server so
the 401/200 paths are exercised over real HTTP rather than mocks.
"""
from __future__ import annotations

import http.server
import json
import socket
import sys
import threading
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime import preflight_gateway_authentication

GOOD_KEY = "sk-ac6-THIS-IS-THE-ACCEPTED-TEST-KEY-00"
BAD_KEY = "sk-ac6-THIS-KEY-IS-REJECTED-BY-THE-GATEWAY"
FAST_MODEL = "openrouter/z-ai/glm-5.3-flash"


class _StubGateway(http.server.BaseHTTPRequestHandler):
    """Minimal 9router-shaped gateway: 401 unless the accepted key is sent."""

    accepted_key = GOOD_KEY
    models = [FAST_MODEL, "openrouter/qwen/qwen3.7-flash"]

    def do_GET(self):  # noqa: N802 - http.server API
        if not self.path.endswith("/models"):
            self.send_error(404)
            return
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {self.accepted_key}":
            body = b'{"error":"API key required for remote API access"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        payload = json.dumps({"data": [{"id": m} for m in self.models]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence per-request stderr noise
        return


@pytest.fixture()
def gateway():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)



def _rendered(caplog):
    """Safely render log records to text (records may already be formatted)."""
    out = []
    for r in caplog.records:
        try:
            out.append(r.getMessage())
        except Exception:
            out.append(str(r.msg))
    return "\n".join(out)


class _Cfg:
    def __init__(self, home: Path):
        self.hermes_home = home


def write_profile(home: Path, base_url: str, *, dotenv: str | None,
                  fast_model: str = FAST_MODEL, provider: str = "custom:openai-api"):
    home.mkdir(parents=True, exist_ok=True)
    short = provider.split(":", 1)[1] if ":" in provider else provider
    profile = {
        "model": {"default": "frontend", "provider": provider},
        "website_builder": {
            "models": {
                "FAST": {"model": fast_model, "provider": provider},
                "FRONTEND": {"model": fast_model, "provider": provider},
                "VISION": {"model": "openrouter/qwen/qwen3.7-flash",
                           "provider": provider},
            }
        },
        "providers": {
            short: {
                "base_url": base_url,
                "key_env": "NINEROUTER_API_KEY",
                "models": {},
                "default_model": "frontend",
            }
        },
    }
    (home / "config.yaml").write_text(yaml.safe_dump(profile))
    if dotenv is not None:
        (home / ".env").write_text(dotenv)
    return home


class TestGatewayAuthenticationPreflight:
    def test_accepted_key_allows_startup(self, gateway, tmp_path):
        home = write_profile(tmp_path / "p", gateway,
                             dotenv=f"NINEROUTER_API_KEY={GOOD_KEY}\n")
        assert preflight_gateway_authentication(_Cfg(home)) is True

    def test_rejected_key_refuses_startup(self, gateway, tmp_path, caplog):
        home = write_profile(tmp_path / "p", gateway,
                             dotenv=f"NINEROUTER_API_KEY={BAD_KEY}\n")
        with caplog.at_level("ERROR"):
            assert preflight_gateway_authentication(_Cfg(home)) is False
        assert "REJECTED" in _rendered(caplog)

    def test_missing_key_refuses_startup(self, gateway, tmp_path, caplog):
        home = write_profile(tmp_path / "p", gateway,
                             dotenv="# no credential here\n")
        with caplog.at_level("ERROR"):
            assert preflight_gateway_authentication(_Cfg(home)) is False
        assert "not configured" in _rendered(caplog)

    def test_absent_dotenv_refuses_startup(self, gateway, tmp_path):
        home = write_profile(tmp_path / "p", gateway, dotenv=None)
        assert preflight_gateway_authentication(_Cfg(home)) is False

    def test_unserved_fast_model_refuses_startup(self, gateway, tmp_path, caplog):
        home = write_profile(tmp_path / "p", gateway,
                             dotenv=f"NINEROUTER_API_KEY={GOOD_KEY}\n",
                             fast_model="openrouter/retired/model-that-is-gone")
        with caplog.at_level("ERROR"):
            assert preflight_gateway_authentication(_Cfg(home)) is False
        assert "not serve" in _rendered(caplog)

    def test_unreachable_gateway_refuses_startup(self, tmp_path):
        # Bind then close, so the port is almost certainly dead.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        home = write_profile(tmp_path / "p", f"http://127.0.0.1:{port}/v1",
                             dotenv=f"NINEROUTER_API_KEY={GOOD_KEY}\n")
        assert preflight_gateway_authentication(_Cfg(home)) is False

    def test_non_http_provider_is_skipped_not_failed(self, tmp_path):
        home = write_profile(tmp_path / "p", "sdk://not-a-gateway",
                             dotenv=None)
        assert preflight_gateway_authentication(_Cfg(home)) is True

    def test_key_never_appears_in_log_output(self, gateway, tmp_path, caplog):
        home = write_profile(tmp_path / "p", gateway,
                             dotenv=f"NINEROUTER_API_KEY={BAD_KEY}\n")
        with caplog.at_level("DEBUG"):
            preflight_gateway_authentication(_Cfg(home))
        rendered = _rendered(caplog)
        assert BAD_KEY not in rendered
        assert GOOD_KEY not in rendered
