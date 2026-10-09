"""D3b self-checks: the FINAL PROOF runner must be honest about itself.

The runner (``tools/d3b_final_proof.py``) pins an expected guard count per
driver. If a driver gains or loses a mutation and the pin is not updated, the
proof would either fail (good) or -- worse -- silently under-report. These
checks parse BOTH the runner's pins and the drivers' own ``MUTATIONS`` lists and
require them to agree, so the two cannot drift apart unnoticed.

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
RUNNER = TOOLS / "d3b_final_proof.py"


def _runner_pins() -> Dict[str, int]:
    """``{driver_filename: expected_count}`` from the runner's own constants."""
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    pins: Dict[str, int] = {}
    d3b_driver = None
    d3b_guards = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign):
            target = node.targets[0] if node.targets else None
            value = node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        if target.id == "D3A5_DRIVERS" and isinstance(value, ast.Tuple):
            for elt in value.elts:
                pins[elt.elts[0].value] = elt.elts[1].value
        elif target.id == "D3B_DRIVER" and isinstance(value, ast.Constant):
            d3b_driver = value.value
        elif target.id == "D3B_DRIVER_GUARDS" and isinstance(value, ast.Constant):
            d3b_guards = value.value
    if d3b_driver is not None:
        pins[d3b_driver] = d3b_guards
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
    for filename in _runner_pins():
        assert (TOOLS / filename).is_file(), filename


def test_the_runner_pins_match_the_drivers_mutation_counts():
    mismatches = {}
    for filename, expected in _runner_pins().items():
        actual = _driver_mutation_count(TOOLS / filename)
        if actual != expected:
            mismatches[filename] = f"pinned {expected}, driver has {actual}"
    assert not mismatches, f"runner pins drifted from the drivers: {mismatches}"


def test_the_d3b_driver_has_fourteen_guards():
    """The D3b batch requires 14 mutation families."""
    assert _driver_mutation_count(TOOLS / "mutation_check_d3b.py") == 14


def test_the_runner_covers_the_five_d3a5_drivers():
    pins = _runner_pins()
    d3a5 = {f for f in pins if "d3a5" in f}
    assert d3a5 == {
        "mutation_check_d3a5_parta.py",
        "mutation_check_d3a5_partbc.py",
        "mutation_check_d3a5_partc.py",
        "mutation_check_d3a5_partd.py",
        "mutation_check_d3a5_parti.py",
    }


def test_the_runner_includes_the_d3b_driver():
    pins = _runner_pins()
    assert "mutation_check_d3b.py" in pins
    assert pins["mutation_check_d3b.py"] == 14


def test_the_runner_blocks_non_loopback_network():
    src = RUNNER.read_text(encoding="utf-8")
    assert "_socket.socket.connect = _connect" in src
    assert "_socket.getaddrinfo = _getaddrinfo" in src
    assert "NETWORK BLOCKED" in src
    assert "loopback" in src.lower()


def test_the_runner_does_not_weaken_the_d3a5_phase_boundary():
    """The D3b runner must NOT call the D3a.5 runner (whose 'no D3b artifacts'
    assertion is correct for that phase). It runs the D3a.5 drivers directly."""
    src = RUNNER.read_text(encoding="utf-8")
    # It names the D3a.5 DRIVERS, never the D3a.5 RUNNER.
    assert "mutation_check_d3a5" in src
    assert "d3a5_final_proof" not in src.replace("tools/d3a5_final_proof.py", "")
