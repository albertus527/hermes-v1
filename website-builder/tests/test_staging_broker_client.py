"""Broker client transport tests (UNIX socket -> broker).

Uses a tiny in-test AF_UNIX server that mimics the broker's policy responses,
so no real network or token is involved.
"""
import json
import os
import socket
import tempfile
import threading

import pytest

from app.deploy.adapters import HttpResponse
from app.deploy.staging_broker_client import (
    BrokerTransport,
    broker_socket_path,
    build_broker_transport,
)


class _FakeBroker(threading.Thread):
    """Minimal AF_UNIX server returning scripted responses."""

    def __init__(self, path, responder):
        super().__init__(daemon=True)
        self.path = path
        self.responder = responder
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(4)
        self.received = []

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                raw = b""
                while not raw.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                self.received.append(json.loads(raw.decode()))
                conn.sendall((json.dumps(self.responder(self.received[-1])) + "\n").encode())

    def stop(self):
        self.sock.close()


@pytest.fixture
def broker():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "v.sock")
        responders = []
        b = _FakeBroker(path, lambda msg: responders[0])
        b.start()
        yield b, path, responders
        b.stop()


def test_broker_socket_path_only_when_set():
    assert broker_socket_path({}) is None
    assert broker_socket_path({"WEBSITE_BUILDER_VERCEL_BROKER": "/run/x.sock"}) == "/run/x.sock"
    # relative value -> default path
    assert broker_socket_path({"WEBSITE_BUILDER_VERCEL_BROKER": "1"}) == "/run/wb-e2e/vercel.sock"


def test_build_transport_none_without_env():
    assert build_broker_transport({}) is None


def test_transport_forwards_and_decomposes(broker):
    b, path, responders = broker
    responders.append({"__status__": 200, "id": "prj_1"})
    tr = BrokerTransport(path)
    resp = tr.request("GET", "https://api.vercel.com/v9/projects/wb-x?teamId=team_1",
                      {"Authorization": "Bearer broker-managed"})
    assert isinstance(resp, HttpResponse) and resp.status == 200
    assert json.loads(resp.body)["id"] == "prj_1"
    sent = b.received[-1]
    assert sent["method"] == "GET" and sent["path"] == "/v9/projects/wb-x"
    assert sent["query"] == {"teamId": "team_1"}


def test_transport_surfaces_guard_denial_as_403(broker):
    b, path, responders = broker
    responders.append({"error": {"code": "STAGING_BROKER_DENIED", "reason": "denied_operation"}})
    tr = BrokerTransport(path)
    resp = tr.request("POST", "https://api.vercel.com/v10/projects/p/promote/d", {"Authorization": "x"})
    assert resp.status == 403
    assert json.loads(resp.body)["error"]["reason"] == "denied_operation"


def test_transport_refuses_non_http_target():
    tr = BrokerTransport("/nonexistent.sock")
    with pytest.raises(ValueError):
        tr.request("GET", "file:///etc/passwd", {})


def test_transport_rejects_non_json_payload(broker):
    b, path, responders = broker
    tr = BrokerTransport(path)
    with pytest.raises(ValueError):
        tr.request("POST", "https://api.vercel.com/v11/projects", {}, data=b"not json")


def test_adapter_uses_broker_transport_when_env_set(broker, monkeypatch):
    b, path, responders = broker
    responders.append({"__status__": 200, "id": "prj_1", "name": "n", "accountId": "team_1",
                       "env": []})
    from app.deploy.staging_guard import build_vercel_adapter
    a = build_vercel_adapter(token="broker-managed", team_id="team_1",
                             ownership_namespace="ns",
                             env={"WEBSITE_BUILDER_VERCEL_BROKER": path})
    # plain adapter (guard off) still routes through the broker socket
    assert isinstance(a.transport, BrokerTransport)
