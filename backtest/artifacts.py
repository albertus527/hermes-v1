"""Versioned R2.7 strategy artifacts: universe.yaml / fee_schedule.yaml.

Artifacts live under ``$HERMES_HOME/backtest/artifacts/`` (profile-aware
via hermes_constants.get_hermes_home). The seeds committed in this repo
(backtest/seeds/) are copied into place on first use; user edits
(Pluang confirmations, verified fee-rate research) increment the version.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from hermes_constants import get_hermes_home

SEEDS_DIR = Path(__file__).parent / "seeds"
UNIVERSE_SEED = SEEDS_DIR / "universe.yaml"
FEE_SCHEDULE_SEED = SEEDS_DIR / "fee_schedule.yaml"


def artifacts_dir() -> Path:
    return Path(get_hermes_home()) / "backtest" / "artifacts"


def ensure_artifacts() -> Path:
    """Materialize the seed artifacts under HERMES_HOME if absent."""
    dest = artifacts_dir()
    dest.mkdir(parents=True, exist_ok=True)
    for seed in (UNIVERSE_SEED, FEE_SCHEDULE_SEED):
        target = dest / seed.name
        if not target.exists():
            shutil.copyfile(seed, target)
    return dest
