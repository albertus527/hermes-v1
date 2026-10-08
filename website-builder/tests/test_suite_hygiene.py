"""Suite hygiene: latent source defects that a normal run would not fail on.

Two properties, both instances of the batch's recurring class -- enforcement
weaker than the property it names:

1. **A test function name must be defined exactly ONCE per file.** A duplicate
   ``def test_x`` is a silent coverage hole: Python binds only the LAST
   definition, so every earlier body becomes dead code that pytest never
   collects while the file still reads as if it had them. Two real instances
   were found and fixed:

   * ``tests/test_design_transitions.py`` defined
     ``test_a_symlinked_recipes_dir_escaping_the_project_is_refused`` THREE times
     (a paste at ``74fc55e6c``). Two bodies were dead -- one carrying a latent
     ``_symlinks_available(tmp_path)`` arity bug that never ran.
   * ``tests/test_design_critic.py`` defined
     ``test_containment_is_measured_against_the_resolved_root`` twice (identical
     bodies, so no coverage was lost, but the same paste defect).

2. **No source file may contain an invalid escape sequence.** A non-raw string
   literal holding ``\\s`` / ``\\(`` / ``\\{`` compiles today with a
   ``SyntaxWarning`` and becomes a hard ``SyntaxError`` in a future Python. A
   real instance: ``tools/mutation_check_d3a5_partd.py`` held two such anchors
   (the reduced-motion regex snippets). The warning was the only signal; a
   normal ``pytest`` run does not fail on it, so it could sit unnoticed until an
   interpreter upgrade turned it into a collection error.
"""

from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TESTS_DIR = Path(__file__).resolve().parent
BUILDER = Path(__file__).resolve().parents[1]

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


# ---------------------------------------------------------------------------
# Invalid escape sequences in any shipped source
# ---------------------------------------------------------------------------


def _source_files() -> List[Path]:
    """Every ``.py`` this project ships or runs: app/, tests/, tools/."""
    files: List[Path] = []
    for sub in ("app", "tests", "tools"):
        files.extend((BUILDER / sub).rglob("*.py"))
    return sorted(files)


def _invalid_escapes(path: Path) -> List[Tuple[int, str]]:
    """``(lineno, message)`` for each invalid escape in ``path``.

    Uses ``compile`` under a warnings filter rather than a regex, so it sees
    exactly what the interpreter sees -- including a warning that only fires when
    the literal is actually compiled.
    """
    source = path.read_text(encoding="utf-8")
    found: List[Tuple[int, str]] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as error:  # a hard failure is also a finding
            return [(error.lineno or 0, f"SyntaxError: {error.msg}")]
        for item in caught:
            if issubclass(item.category, SyntaxWarning):
                found.append((item.lineno or 0, str(item.message)))
    return found


def test_no_source_file_has_an_invalid_escape_sequence():
    """A non-raw string with ``\\s`` compiles today, breaks on a future Python.

    The fix is a raw string (``r"..."``) when the backslash is intended, or a
    doubled backslash when it is not. This guard makes the latent breakage fail
    at the point it is introduced instead of at an interpreter upgrade.
    """
    offenders: Dict[str, List[str]] = {}
    for path in _source_files():
        problems = _invalid_escapes(path)
        if problems:
            offenders[str(path.relative_to(BUILDER))] = [
                f"line {line}: {message}" for line, message in problems
            ]

    assert not offenders, (
        "these source files contain an invalid escape sequence, which is a "
        f"SyntaxWarning today and a SyntaxError in a future Python: {offenders}"
    )


def test_the_escape_scanner_would_catch_a_real_one(tmp_path):
    """The guard itself is load-bearing: prove it flags a planted invalid escape.

    Without this, a scanner that always returned ``[]`` would make the guard
    above vacuous.
    """
    sample = tmp_path / "sample.py"
    # A non-raw literal with an invalid escape -- exactly the defect class.
    sample.write_text('PATTERN = "a\\sb"\n', encoding="utf-8")

    assert _invalid_escapes(sample), "the scanner missed a planted invalid escape"

    # And a correctly raw one is clean, so the guard is not merely permissive.
    clean = tmp_path / "clean.py"
    clean.write_text('PATTERN = r"a\\sb"\n', encoding="utf-8")
    assert not _invalid_escapes(clean)


def test_the_escape_scanner_sees_every_source_tree():
    """The scan must cover app/, tests/ AND tools/ -- where the real one lived."""
    files = {str(p.relative_to(BUILDER)) for p in _source_files()}

    assert any(f.startswith("app/") for f in files)
    assert any(f.startswith("tests/") for f in files)
    assert any(f.startswith("tools/") for f in files)
    # The file that actually carried the defect must be in scope.
    assert "tools/mutation_check_d3a5_partd.py" in files

