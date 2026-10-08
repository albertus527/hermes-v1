"""Part M self-checks: the FINAL PROOF runner must be honest about itself.

The runner (`tools/d3a5_final_proof.py`) pins an expected guard count per driver.
If a driver gains or loses a mutation and the pin is not updated, the proof would
either fail (good) or -- worse -- silently under-report. These checks parse BOTH
the runner's pins and the drivers' own ``MUTATIONS`` lists and require them to
agree, so the two cannot drift apart unnoticed.

No network, no subprocess, no mutation: pure static cross-checks.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Dict, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BUILDER = Path(__file__).resolve().parents[1]
TOOLS = BUILDER / "tools"
RUNNER = TOOLS / "d3a5_final_proof.py"


def _runner_pins() -> Dict[str, int]:
    """``{driver_filename: expected_count}`` from the runner's own tuples."""
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    pins: Dict[str, int] = {}
    for node in ast.walk(tree):
        # The runner annotates these (``D3A5_DRIVERS: Tuple[...] = (...)``), so
        # they are AnnAssign, not plain Assign.
        if isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        elif isinstance(node, ast.Assign):
            target = node.targets[0] if node.targets else None
            value = node.value
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id in (
            "D3A5_DRIVERS", "LEGACY_DRIVERS",
        )):
            continue
        for elt in value.elts:
            filename = elt.elts[0].value
            count = elt.elts[1].value
            pins[filename] = count
    return pins


def _driver_mutation_count(path: Path) -> int:
    """The number of entries in a driver's ``MUTATIONS`` list."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MUTATIONS":
                    return len(node.value.elts)
    raise AssertionError(f"no MUTATIONS list in {path}")


def test_the_runner_pins_every_driver_it_runs():
    """Every driver the runner names must exist."""
    for filename in _runner_pins():
        assert (TOOLS / filename).is_file(), filename


def test_the_runner_pins_match_the_drivers_mutation_counts():
    """The pinned count must equal the driver's real mutation count.

    This is the check that makes the runner's verdict meaningful: a guard added
    to a driver without updating the pin (or a pin typo) fails here.
    """
    mismatches = {}
    for filename, expected in _runner_pins().items():
        actual = _driver_mutation_count(TOOLS / filename)
        if actual != expected:
            mismatches[filename] = f"pinned {expected}, driver has {actual}"
    assert not mismatches, f"runner pins drifted from the drivers: {mismatches}"


def test_the_runner_covers_the_five_d3a5_drivers():
    """The D3a.5 gate must name exactly the five batch drivers."""
    pins = _runner_pins()
    d3a5 = {f for f in pins if "d3a5" in f}
    assert d3a5 == {
        "mutation_check_d3a5_parta.py",
        "mutation_check_d3a5_partbc.py",
        "mutation_check_d3a5_partc.py",
        "mutation_check_d3a5_partd.py",
        "mutation_check_d3a5_parti.py",
    }


def test_the_runner_blocks_non_loopback_network():
    """The offline proof must actually block the network it claims to."""
    src = RUNNER.read_text(encoding="utf-8")
    # The netblock payload is embedded; it must override the real syscalls.
    assert "_socket.socket.connect = _connect" in src
    assert "_socket.getaddrinfo = _getaddrinfo" in src
    assert "NETWORK BLOCKED" in src
    assert "loopback" in src.lower()
