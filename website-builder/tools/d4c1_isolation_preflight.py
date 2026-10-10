#!/usr/bin/env python3
"""D4c.1 isolation preflight (ZERO paid calls).

Verifies that a REAL HermesAdapter built against an ISOLATED temp HERMES_HOME
resolves the FAST role end-to-end (config + credentials + provider), so the paid
smoke can run without touching the production website profile state
(no state.db session rows written to ~/.hermes-website).
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.state import ProjectStateStore  # noqa: E402
from app.hermes.adapter import HermesAdapter  # noqa: E402

PROFILE = Path.home() / ".hermes-website"


def make_isolated_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="d4c1-home-"))
    shutil.copy2(PROFILE / "config.yaml", home / "config.yaml")
    # copy only the secret env file (key names preserved; values never printed)
    if (PROFILE / ".env").exists():
        shutil.copy2(PROFILE / ".env", home / ".env")
        (home / ".env").chmod(0o600)
    return home


def main() -> int:
    home = make_isolated_home()
    store = ProjectStateStore(Path(tempfile.mkdtemp(prefix="d4c1-state-")))
    adapter = HermesAdapter(store=store, hermes_home=home, repo_root=ROOT)
    res = adapter.validate_role_configuration()
    print(json.dumps({
        "isolated_home": str(home),
        "config_present": (home / "config.yaml").exists(),
        "env_present": (home / ".env").exists(),
        "roles_ok": res.get("ok"),
        "fast": res.get("roles", {}).get("FAST"),
        "errors": res.get("errors"),
        "isolated_state_db_exists_after_preflight": (home / "state.db").exists(),
    }, indent=2, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
