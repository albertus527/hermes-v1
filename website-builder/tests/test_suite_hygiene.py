"""Suite hygiene: a test function name must be defined exactly ONCE per file.

A duplicate ``def test_x`` is a silent coverage hole: Python binds only the LAST
definition, so every earlier body becomes dead code that pytest never collects
while the file still reads as if it had them. Two real instances were found and
fixed:

  * ``tests/test_design_transitions.py`` defined
    ``test_a_symlinked_recipes_dir_escaping_the_project_is_refused`` THREE times
    (a paste at ``74fc55e6c``). Two bodies were dead -- one carrying a latent
    ``_symlinks_available(tmp_path)`` arity bug that never ran.
  * ``tests/test_design_critic.py`` defined
    ``test_containment_is_measured_against_the_resolved_root`` twice (identical
    bodies, so no coverage was lost, but the same paste defect).

This is the batch's recurring class: enforcement weaker than the property it
names. A test that is defined but never collected is not a test. This guard makes
the duplicate unrepresentable: it scans every ``tests/test_*.py`` for a repeated
``def test_*`` name and fails with the file and the name.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TESTS_DIR = Path(__file__).resolve().parent

#: A top-level test function definition. Anchored at column 0 so a nested helper
#: inside a test class (indented) is not mistaken for a module-level test.
_DEF_RE = re.compile(r"^def (test_[A-Za-z0-9_]+)\s*\(", re.MULTILINE)


def _duplicate_test_names(path: Path) -> List[str]:
    names = _DEF_RE.findall(path.read_text(encoding="utf-8"))
    seen: Dict[str, int] = {}
    for name in names:
        seen[name] = seen.get(name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)


def _test_files() -> List[Path]:
    return sorted(TESTS_DIR.glob("test_*.py"))


def test_no_test_file_defines_a_name_twice():
    """A duplicate test name silently shadows every earlier definition."""
    offenders: Dict[str, List[str]] = {}
    for path in _test_files():
        dups = _duplicate_test_names(path)
        if dups:
            offenders[path.name] = dups

    assert not offenders, (
        "these test files define the same test name more than once, so the "
        f"earlier definitions are never collected: {offenders}"
    )


def test_the_scanner_would_catch_a_duplicate(tmp_path):
    """The guard itself is load-bearing: prove it flags a real duplicate.

    Without this, a scanner bug that always returned ``[]`` would make the guard
    above vacuous.
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "def test_alpha():\n    pass\n\n\n"
        "def test_alpha():\n    pass\n\n\n"
        "def test_beta():\n    pass\n",
        encoding="utf-8",
    )

    assert _duplicate_test_names(sample) == ["test_alpha"]


def test_the_scanner_sees_every_test_file():
    """A non-empty, growing set -- so the guard cannot pass by scanning nothing."""
    files = _test_files()

    assert len(files) > 50, f"expected the full suite, saw {len(files)} files"
    # The two files that actually carried a duplicate must be in scope.
    names = {p.name for p in files}
    assert "test_design_transitions.py" in names
    assert "test_design_critic.py" in names
