"""Broker transport: talk to the privileged Vercel broker over a UNIX socket.

The staging runtime runs as unprivileged ``wbe2e`` and spawns untrusted
children (FRONTEND agent, generated npm build, Chromium) as the SAME uid. A
deploy token stored anywhere the runtime can read is therefore readable by
that untrusted code. This transport lets the runtime perform the Vercel
operations it needs WITHOUT ever holding the token:

    VercelAdapter --(https URL)--> BrokerTransport --(unix socket)--> broker
                                                                      |
                                                        (real token, root-only)
                                                                      v
                                                              api.vercel.com

The token lives only in the broker process (see ``bin/vercel_broker.py``,
run as a separate root unit outside the staging cgroup). Combined with a
cgroup-scoped nft rule dropping ``tcp dport 443`` for the staging unit, a
token leaked to untrusted code is useless AND a direct API call is blocked.

Activated only when ``WEBSITE_BUILDER_VERCEL_BROKER`` names a socket path.
"""
from __future__ import annotations

import json
import os
import socket
from typing import Mapping, Optional
from urllib.parse import parse_qsl, urlparse

from app.deploy.adapters import HttpResponse

BROKER_ENV = "WEBSITE_BUILDER_VERCEL_BROKER"
DEFAULT_SOCK = "/run/wb-e2e/vercel.sock"


def broker_socket_path(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    raw = os.environ if env is None else env
    value = str(raw.get(BROKER_ENV, "")).strip()
    if not value:
        return None
    return value if os.path.isabs(value) else DEFAULT_SOCK


class BrokerTransport:
    """``UrllibHttpTransport``-compatible transport that proxies via the broker.

    It exposes the same ``request(method, url, headers, data, timeout)``
    contract, so ``VercelAdapter`` needs no change. The provider URL is
    decomposed into method/path/query/payload and sent to the broker, which
    validates the operation server-side before touching the real API.
    """

    def __init__(self, sock_path: str, *, timeout: float = 30.0):
        self.sock_path = sock_path
        self.timeout = timeout

    def request(self, method, url, headers, data=None, timeout=30):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("broker transport: refusing non-HTTP target")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        payload = None
        if data:
            try:
                payload = json.loads(data.decode())
            except Exception:
                raise ValueError("broker transport: payload must be JSON")
        message = json.dumps({
            "method": str(method).upper(),
            "path": parsed.path,
            "query": query,
            "payload": payload,
        }).encode() + b"\n"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout or self.timeout)
        try:
            sock.connect(self.sock_path)
            sock.sendall(message)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        raw = b"".join(chunks).decode("utf-8", "replace").strip()
        try:
            body = json.loads(raw)
        except Exception:
            raise ValueError("broker transport: malformed broker response")
        if not isinstance(body, dict):
            raise ValueError("broker transport: broker response not an object")
        error = body.get("error") if isinstance(body.get("error"), dict) else None
        if error and error.get("code") in ("STAGING_BROKER_DENIED", "BAD_REQUEST"):
            # Surface as a non-2xx so callers fail closed on a guard denial.
            return HttpResponse(403, json.dumps(body).encode())
        if error and error.get("code") == "UPSTREAM_ERROR":
            return HttpResponse(502, json.dumps(body).encode())
        status = int(body.pop("__status__", 200)) if "__status__" in body else 200
        return HttpResponse(status, json.dumps(body).encode())


def build_broker_transport(env: Optional[Mapping[str, str]] = None) -> Optional[BrokerTransport]:
    path = broker_socket_path(env)
    return BrokerTransport(path) if path else None
