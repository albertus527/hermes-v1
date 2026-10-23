#!/usr/bin/env python3
"""TASK 4 -- provision an ISOLATED R2 development profile.

Creates a dedicated R2 profile tree that shares NOTHING with R1. Every
mutable path is a fresh directory under one R2 root. Nothing is copied from
R1 except the NON-SECRET role configuration (model ids and provider names),
because those are ordinary settings, not credentials.

WHAT IS DELIBERATELY NOT DONE
  * The R1 Telegram bot token is NOT copied. A placeholder is written instead,
    clearly marked, so the runtime can load config for a READ-ONLY preflight
    but can never poll Telegram.
  * No listener is started.
  * R1 state, workspaces and output repo are never opened for writing.
  * The R1 MODEL GATEWAY credential (e.g. ``NINEROUTER_API_KEY``) is NOT
    copied either. This script only CHECKS whether the new profile can
    authenticate to its gateway and reports the exact operator action when it
    cannot. Distributing an R1 credential to every future profile is an
    operator decision, not a side effect of provisioning.

WHY THE GATEWAY CREDENTIAL IS A SPECIAL CASE
  The profile ``.env`` is where the model key legitimately lives: the
  ``hermes -z`` child loads ``$HERMES_HOME/.env`` with ``override=True``, and
  the model credential allowlist that scopes the child's process environment
  does NOT include a self-hosted gateway's key. So a gateway key in
  ``app.env`` alone never reaches the child, while one in the profile ``.env``
  does. It is a MODEL credential (not a deploy/channel credential), so the
  profile guard permits it — but the child holds file and terminal tools, so
  the exposure is real and must be accepted deliberately.

The script only writes inside the R2 root. It prints what it did, never a
credential value.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

R2_ROOT = Path.home() / ".website-builder-r2"
R1_PROFILE = Path.home() / ".hermes-website"

#: Sentinel that can never authenticate. Deliberately invalid so that any
#: accidental polling attempt fails closed at Telegram rather than connecting.
TELEGRAM_PLACEHOLDER = "000000000:R2-PLACEHOLDER-DO-NOT-POLL"

LAYOUT = {
    "profile": R2_ROOT / "profile",          # HERMES_HOME
    "state": R2_ROOT / "state",              # WEBSITE_BUILDER_STATE_ROOT
    "workspaces": R2_ROOT / "workspaces",    # WEBSITE_BUILDER_WORKSPACE_ROOT
    "output_repo": R2_ROOT / "output-repo",  # WEBSITE_BUILDER_OUTPUT_REPO
    "logs": R2_ROOT / "logs",
}


def _read_key_env(profile_cfg: dict) -> tuple[str, str]:
    """Return (base_url, key_env) for the FAST role's provider, or ('','')."""
    providers = profile_cfg.get("providers") or {}
    role_models = (profile_cfg.get("website_builder") or {}).get("models") or {}
    fast = role_models.get("FAST") if isinstance(role_models, dict) else None
    provider_name = ""
    if isinstance(fast, dict):
        provider_name = str(fast.get("provider") or "").strip()
    if not provider_name:
        provider_name = str((profile_cfg.get("model") or {}).get("provider") or "").strip()
    short = provider_name.split(":", 1)[1] if ":" in provider_name else provider_name
    entry = providers.get(short) if isinstance(providers, dict) else None
    if not isinstance(entry, dict):
        return "", ""
    base_url = str(entry.get("base_url") or "").strip().rstrip("/")
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    return base_url, key_env


def check_gateway_credential(profile_cfg: dict, profile_home: Path) -> tuple[bool, str]:
    """Check the new profile can authenticate to its model gateway.

    Returns (ok, message). Never prints or returns a credential value.

    This is a CHECK, not a copy. Provisioning a fresh profile deliberately
    does not inherit R1's gateway key: that is an operator decision with a
    real exposure consequence (the generation child holds file and terminal
    tools and can read ``$HERMES_HOME/.env``).
    """
    base_url, key_env = _read_key_env(profile_cfg)
    if not base_url or not key_env:
        return True, "no HTTP gateway declared; nothing to check"
    if not base_url.startswith(("http://", "https://")):
        return True, f"non-HTTP provider ({base_url}); nothing to check"

    dotenv = profile_home / ".env"
    key = ""
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            name, _, value = s.partition("=")
            if name.strip() == key_env:
                key = value.strip().strip("'\"")
                break

    if not key:
        return False, (
            f"{key_env} is not set in {dotenv}. The gateway requires it; "
            f"without it every model call returns HTTP 401 and the brief is "
            f"lost. Provision it deliberately (see docs/R2_E2E_LAUNCH_RUNBOOK.md)."
        )

    try:
        req = urllib.request.Request(
            base_url + "/models",
            headers={"Authorization": "Bearer " + key},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, (
                f"the gateway REJECTED {key_env} (HTTP {exc.code}). "
                f"The credential in {dotenv} is not accepted."
            )
        return False, f"the gateway returned HTTP {exc.code} at {base_url}/models"
    except Exception as exc:
        return False, f"the gateway at {base_url} is unreachable ({type(exc).__name__})"

    served = [str(m.get("id") or "") for m in (payload.get("data") or [])
              if isinstance(m, dict)]
    role_models = (profile_cfg.get("website_builder") or {}).get("models") or {}
    fast = role_models.get("FAST") if isinstance(role_models, dict) else None
    fast_model = str(fast.get("model") or "").strip() if isinstance(fast, dict) else ""
    if fast_model and served and fast_model not in served:
        return False, (
            f"the gateway authenticated but does not serve the FAST model "
            f"{fast_model!r} ({len(served)} model(s) served)"
        )
    return True, f"{base_url} authenticated ({len(served)} model(s) served)"


def main() -> int:
    print("=" * 78)
    print("TASK 4 -- ISOLATED R2 DEVELOPMENT PROFILE")
    print("=" * 78)
    print(f"R2 root: {R2_ROOT}")

    for name, path in LAYOUT.items():
        path.mkdir(parents=True, exist_ok=True)
        # Owner-only, matching the R1 profile's own 0700 convention.
        os.chmod(path, stat.S_IRWXU)
        print(f"  created {name:12s} {path}  (0700)")

    # ---- profile config: reuse ONLY the non-secret role settings -----------
    r1_cfg_path = R1_PROFILE / "config.yaml"
    r2_cfg: dict = {}
    role_block = {}
    if r1_cfg_path.exists():
        r1 = yaml.safe_load(r1_cfg_path.read_text(encoding="utf-8")) or {}
        role_block = ((r1.get("website_builder") or {}).get("models") or {})
        # Copy the provider/credential-free structure only.
        for key in ("providers",):
            if key in r1:
                r2_cfg[key] = r1[key]
        for key in ("model",):
            if key in r1:
                r2_cfg[key] = r1[key]

    r2_cfg.setdefault("website_builder", {})
    r2_cfg["website_builder"]["models"] = role_block

    cfg_out = LAYOUT["profile"] / "config.yaml"
    cfg_out.write_text(yaml.safe_dump(r2_cfg, sort_keys=False), encoding="utf-8")
    os.chmod(cfg_out, 0o600)
    print(f"  wrote    profile/config.yaml  ({len(role_block)} roles, non-secret only)")

    # ---- profile .env: MUST NOT carry privileged credentials --------------
    #
    # The runtime refuses to start if the generation profile's .env holds a
    # privileged credential, because the `hermes -z` child loads that file
    # unfiltered and its role holds file + terminal tools. A Telegram token
    # there would be readable by the generation agent. So the profile .env is
    # written EMPTY of secrets and the token lives only in the process
    # environment (see activate-r2.sh).
    env_out = LAYOUT["profile"] / ".env"
    env_out.write_text(
        "# R2 development profile -- intentionally carries NO credentials.\n"
        "# Privileged credentials belong to the application process environment,\n"
        "# never to the generation profile (the runtime enforces this).\n",
        encoding="utf-8",
    )
    os.chmod(env_out, 0o600)
    print("  wrote    profile/.env  (no secrets -- enforced by the runtime)")

    # ---- an activation script, so the env isolation is explicit -----------
    act = R2_ROOT / "activate-r2.sh"
    act.write_text(
        "#!/usr/bin/env bash\n"
        "# Source this to enter the isolated R2 environment.\n"
        "# Every mutable path points under ~/.website-builder-r2 -- R1 is untouched.\n"
        f'export HERMES_HOME="{LAYOUT["profile"]}"\n'
        f'export WEBSITE_BUILDER_STATE_ROOT="{LAYOUT["state"]}"\n'
        f'export WEBSITE_BUILDER_WORKSPACE_ROOT="{LAYOUT["workspaces"]}"\n'
        f'export WEBSITE_BUILDER_OUTPUT_REPO="{LAYOUT["output_repo"]}"\n'
        "# R2 has no Vercel credential of its own yet; a placeholder keeps\n"
        "# config loading possible for READ-ONLY preflight only.\n"
        'export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-000000000:R2-PLACEHOLDER-DO-NOT-POLL}"\n'
        'export VERCEL_TOKEN="${VERCEL_TOKEN:-R2-PLACEHOLDER-NO-DEPLOY}"\n'
        'export VERCEL_TEAM_ID="${VERCEL_TEAM_ID:-R2-PLACEHOLDER-TEAM}"\n'
        'export R2_DEV_PROFILE=1\n'
        'echo "R2 isolated env active: $HERMES_HOME"\n'
        'echo "  state:      $WEBSITE_BUILDER_STATE_ROOT"\n'
        'echo "  workspaces: $WEBSITE_BUILDER_WORKSPACE_ROOT"\n'
        'echo "  output:     $WEBSITE_BUILDER_OUTPUT_REPO"\n',
        encoding="utf-8",
    )
    os.chmod(act, 0o700)
    print(f"  wrote    {act.name}  (0700)")

    print()
    print("ISOLATION SUMMARY")
    r1_paths = {
        "state": Path.home() / ".website-builder" / "state",
        "workspaces": Path.home() / "website-workspaces",
        "output": Path.home() / ".website-builder" / "output-repo",
        "profile": R1_PROFILE,
    }
    ok = True
    for name, r2p in (("state", LAYOUT["state"]), ("workspaces", LAYOUT["workspaces"]),
                      ("output", LAYOUT["output_repo"]), ("profile", LAYOUT["profile"])):
        r1p = r1_paths[name]
        disjoint = not (str(r2p).startswith(str(r1p)) or str(r1p).startswith(str(r2p)))
        print(f"  {name:11s} R2={r2p}")
        print(f"  {'':11s} R1={r1p}   disjoint={disjoint}")
        ok = ok and disjoint

    # Confirm the R1 token never landed in the R2 profile.
    r1_env = R1_PROFILE / ".env"
    r1_token = ""
    if r1_env.exists():
        for line in r1_env.read_text(errors="ignore").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                r1_token = line.split("=", 1)[1].strip()
    leaked = False
    if r1_token:
        for f in LAYOUT["profile"].rglob("*"):
            if f.is_file() and f.stat().st_size < 200_000:
                try:
                    if r1_token in f.read_text(errors="ignore"):
                        leaked = True
                except OSError:
                    pass
    print()
    print(f"  R1 telegram token present in R2 profile: {leaked}  (must be False)")

    # ---- gateway credential: CHECK, never copy ----------------------------
    gw_ok, gw_msg = check_gateway_credential(r2_cfg, LAYOUT["profile"])
    print()
    print("MODEL GATEWAY CREDENTIAL")
    print(f"  {gw_msg}")
    if not gw_ok:
        print("  This profile CANNOT start a build until that is resolved:")
        print("  the first model call would return HTTP 401 and the brief")
        print("  would be lost. Provision the key deliberately — this script")
        print("  will not copy an R1 credential into a new profile.")

    print()
    verdict_ok = ok and not leaked and gw_ok
    print("VERDICT:", "PASS" if verdict_ok else "FAIL")
    if not verdict_ok:
        print("  (isolation OK)" if ok and not leaked else "  (isolation FAILED)")
        print("  (gateway credential OK)" if gw_ok else "  (gateway credential MISSING/REJECTED)")
    return 0 if verdict_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
