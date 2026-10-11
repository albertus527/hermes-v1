"""CLI entrypoint: ``python -m backtest.calibration_reviewer``.

Starts a loopback-only reviewer. There is deliberately NO public-bind option.
The one-time pairing code is printed to this terminal only; it is never put in
a URL, logged elsewhere, or written to disk.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from backtest.calibration_reviewer.schema import REVIEWER_DIR, REVIEWER_DB_NAME
from backtest.calibration_reviewer.server import ReviewerApp, build_server

DEFAULT_PORT = 8765
DEFAULT_CACHE = os.path.expanduser("~/.hermes/backtest/backtest.sqlite3")


def _default_db_path() -> Path:
    root = os.environ.get("HERMES_HOME")
    if root:
        return Path(root) / "backtest" / "calibration" / "reviewer" / REVIEWER_DB_NAME
    return REVIEWER_DIR / REVIEWER_DB_NAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.calibration_reviewer",
        description="R2.8.1 §11.4 human calibration reviewer (loopback only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db-path", default=str(_default_db_path()),
                        help="reviewer SQLite path (never the backtest store)")
    parser.add_argument("--cache-db", default=DEFAULT_CACHE,
                        help="production news cache, opened read-only for reveal")
    parser.add_argument("--no-cache", action="store_true",
                        help="disable model-prediction reveal entirely")
    args = parser.parse_args(argv)

    if args.port < 1 or args.port > 65535:
        print("invalid --port", file=sys.stderr)
        return 2

    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(db_path.parent, 0o700)

    cache = None if args.no_cache else args.cache_db
    app = ReviewerApp(db_path, cache_db_path=cache)
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass

    server = build_server(app, host="127.0.0.1", port=args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print("=" * 68)
    print(" Hermes R2.8.1 — Human Calibration Reviewer (loopback only)")
    print("=" * 68)
    print(f" Open in a local browser (or via SSH port-forward): {url}")
    print(f" Remote:  ssh -L {args.port}:127.0.0.1:{args.port} <user>@<vps>")
    print()
    print(" ONE-TIME PAIRING CODE (enters the browser once, never a URL):")
    print(f"     {app.security.pairing_code}")
    print()
    print(" Press Ctrl-C to stop. Stopping revokes nothing automatically;")
    print(" restarting generates a fresh pairing code.")
    print("=" * 68)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[reviewer] shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
