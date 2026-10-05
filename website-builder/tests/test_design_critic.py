"""Batch D3a.5 Part I: the Impeccable critic seam.

Every test drives the seam with an INJECTED runner, so nothing executes. The
properties under test are behaviour contracts:

    * the argv is fixed and has no caller-supplied fragment or repair verb
    * exit 1 is an honest scan FAILURE, never a clean report
    * unparseable / unexpected JSON is a failure, never an empty pass
    * an unresolved engine is a reported absence, never a pass
    * output is bounded and truncation is reported
    * a finding with no rule identity or no description is DROPPED, not coerced
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_critic import (
    CRITIC_ARGV_SUFFIX,
    CRITIC_REASONS,
    EXIT_CLEAN,
    EXIT_FINDINGS,
    EXIT_SCAN_FAILED,
    MAX_FINDINGS,
    CriticOutcome,
    build_critic_argv,
    normalize_finding,
    parse_critic_output,
    resolve_engine_path,
    run_critic_scan,
)
from app.core.design_install import is_contained

#: The interpreter the caller names. Never discovered on PATH, never shimmed.
NODE = "/usr/bin/node"

#: The REAL `detect --json` payload: a BARE ARRAY whose field names come
#: from the shipped `detector/findings.mjs` factory. Read out of the
#: upstream source rather than invented -- a parser written against the
#: wrong keys silently reports zero findings on every real scan.
REAL_FINDING_ARRAY = [
    {
        "antipattern": "text-on-surface",
        "name": "Low contrast text",
        "description": "Body text on the raised surface measures 3.9:1.",
        "severity": "warning",
        "category": "contrast",
        "file": "src/Card.tsx",
        "line": 42,
        "snippet": "color: #8a8f98 on #ffffff",
    },
    {
        "antipattern": "no-preference-guard",
        "name": "Motion without a reduced-motion guard",
        "description": "The transition runs without a prefers-reduced-motion guard.",
        "severity": "note",
        "category": "motion",
        "file": "src/Card.tsx",
        "line": 48,
        "snippet": ".card { transition: transform 240ms; }",
    },
]

#: The legacy alias shape, kept as a tolerance test.
REAL_FINDINGS = {
    "findings": [
        {
            "rule_id": "contrast/text-on-surface",
            "category": "contrast",
            "severity": "warning",
            "finding": "Body text on the raised surface measures 3.9:1.",
            "evidence": "color: #8a8f98 on #ffffff",
            "suggested_action": "Darken the text token to at least 4.5:1.",
        },
        {
            "rule_id": "motion/no-preference-guard",
            "category": "motion",
            "severity": "note",
            "finding": "The transition runs without a reduced-motion guard.",
            "evidence": ".card { transition: transform 240ms; }",
            "suggested_action": "Add a prefers-reduced-motion block.",
        },
    ]
}


class RecordingRunner:
    """Stands in for ``subprocess.run``; records argv, returns a canned result."""

    def __init__(self, stdout="", returncode=0):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        return SimpleNamespace(stdout=self.stdout, returncode=self.returncode, stderr="")

    @property
    def argv(self):
        return self.calls[0][0]

    @property
    def kwargs(self):
        return self.calls[0][1]


@pytest.fixture(autouse=True)
def _no_real_execution(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("this layer never executes an engine in tests")

    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)


@pytest.fixture
def skill_root(tmp_path) -> Path:
    root = tmp_path / "skills" / "impeccable"
    (root / "scripts" / "detector").mkdir(parents=True)
    (root / "scripts" / "detect.mjs").write_text(
        "import { detectCli } from './detector/detect-antipatterns.mjs';\n",
        encoding="utf-8",
    )
    (root / "scripts" / "detector" / "detect-antipatterns.mjs").write_text(
        "export { detectCli };\n", encoding="utf-8"
    )
    return root


# ---------------------------------------------------------------------------
# The argv is fixed
# ---------------------------------------------------------------------------


def test_the_argv_is_the_verified_detect_invocation():
    assert CRITIC_ARGV_SUFFIX == ("detect", "--json", "--quiet")


def test_the_engine_is_argv_element_zero_and_nothing_follows_it(tmp_path):
    argv = build_critic_argv(NODE, tmp_path / "detect.mjs")

    assert argv[0] == NODE
    assert argv[1].endswith("detect.mjs")
    assert argv[2:] == CRITIC_ARGV_SUFFIX


def test_the_argv_names_exactly_one_target():
    """No path argument: the engine scans its own working directory."""
    argv = build_critic_argv(NODE, Path("/x/y/detect.mjs"))

    assert len(argv) == 5


def test_no_interpreter_is_ever_discovered_on_path(tmp_path):
    """A scan with no named interpreter is refused, not guessed at."""
    runner = RecordingRunner(stdout="[]")

    outcome = run_critic_scan(tmp_path / "detect.mjs", runner=runner)

    assert outcome.ok is False
    assert outcome.reasons == ("engine_unavailable",)
    assert runner.calls == [], "nothing may execute without a named interpreter"


@pytest.mark.parametrize("forbidden", ["fix", "--fix", "apply", "audit", "refactor"])
def test_no_repair_verb_is_ever_reachable(forbidden):
    """D3a.5 excludes the D3b repair loop; a critic that edits would be it."""
    assert forbidden not in CRITIC_ARGV_SUFFIX


def test_the_engine_is_never_run_through_a_shell(tmp_path):
    runner = RecordingRunner(stdout="{}")

    run_critic_scan(
        tmp_path / "detect.mjs", node_executable=NODE, runner=runner
    )

    assert runner.kwargs["shell"] is False


def test_a_path_with_shell_metacharacters_stays_one_argv_element(tmp_path):
    evil = tmp_path / "det; rm -rf ~ $(id).mjs"
    runner = RecordingRunner(stdout="[]")

    run_critic_scan(evil, node_executable=NODE, runner=runner)

    assert runner.argv[1] == str(evil)
    assert len(runner.argv) == 5


def test_the_runner_receives_a_bounded_timeout(tmp_path):
    runner = RecordingRunner(stdout="{}")

    run_critic_scan(
        tmp_path / "detect.mjs", node_executable=NODE,
        timeout_seconds=7, runner=runner
    )

    assert runner.kwargs["timeout"] == 7


# ---------------------------------------------------------------------------
# Exit codes are honoured honestly
# ---------------------------------------------------------------------------


def test_real_findings_become_critic_findings():
    """The shipped BARE ARRAY parses, using the shipped field names."""
    outcome = parse_critic_output(json.dumps(REAL_FINDING_ARRAY), EXIT_FINDINGS)

    assert outcome.ok is True
    assert [f.rule_id for f in outcome.findings] == [
        "text-on-surface",
        "no-preference-guard",
    ]


def test_a_real_finding_carries_every_canonical_field():
    """No field of the real upstream shape is silently dropped."""
    outcome = parse_critic_output(json.dumps(REAL_FINDING_ARRAY), EXIT_FINDINGS)

    finding = outcome.findings[0].to_dict()
    assert finding["rule_id"] == "text-on-surface"
    assert finding["category"] == "contrast"
    assert finding["severity"] == "warning"
    assert "Body text on the raised surface measures 3.9:1." in finding["finding"]
    assert finding["evidence"] == "color: #8a8f98 on #ffffff"


def test_a_second_real_finding_keeps_its_rule_and_evidence():
    """Guards the upstream keys a wrong parser would silently miss."""
    outcome = parse_critic_output(json.dumps(REAL_FINDING_ARRAY), EXIT_FINDINGS)

    second = outcome.findings[1].to_dict()
    assert second["rule_id"] == "no-preference-guard"
    assert second["evidence"] == ".card { transition: transform 240ms; }"


def test_an_empty_real_array_is_a_clean_scan():
    outcome = parse_critic_output("[]", EXIT_CLEAN)

    assert outcome.ok is True
    assert outcome.findings == ()


def test_a_clean_scan_is_a_clean_scan():
    outcome = parse_critic_output(json.dumps({"findings": []}), EXIT_CLEAN)

    assert outcome.ok is True
    assert outcome.findings == ()


def test_exit_zero_with_no_output_is_clean():
    """The engine may print nothing when there is nothing to report."""
    outcome = parse_critic_output("", EXIT_CLEAN)

    assert outcome.ok is True
    assert outcome.findings == ()


def test_a_scan_failure_is_never_a_clean_report():
    """Exit 1 must not certify a design the engine could not scan."""
    outcome = parse_critic_output(json.dumps(REAL_FINDINGS), EXIT_SCAN_FAILED)

    assert outcome.ok is False
    assert outcome.findings == ()
    assert "scan_failed" in outcome.reasons


@pytest.mark.parametrize("returncode", [3, 127, 255, -1, 100])
def test_any_unexpected_exit_code_is_a_scan_failure(returncode):
    outcome = parse_critic_output(json.dumps(REAL_FINDINGS), returncode)

    assert outcome.ok is False
    assert "scan_failed" in outcome.reasons


def test_the_findings_exit_code_does_not_invent_findings():
    """Exit 2 with an empty list is empty -- the code alone adds nothing."""
    outcome = parse_critic_output(json.dumps({"findings": []}), EXIT_FINDINGS)

    assert outcome.findings == ()


def test_a_timeout_is_a_scan_failure(tmp_path):
    def explode(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    outcome = run_critic_scan(
        tmp_path / "detect.mjs", node_executable=NODE, runner=explode
    )

    assert outcome.ok is False
    assert "scan_failed" in outcome.reasons


def test_a_missing_executable_is_not_a_crash(tmp_path):
    def explode(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    outcome = run_critic_scan(
        tmp_path / "detect.mjs", node_executable=NODE, runner=explode
    )

    assert outcome.ok is False
    assert "engine_not_executable" in outcome.reasons


# ---------------------------------------------------------------------------
# Unusable output is a failure, never an empty pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stdout", ["not json", "{", "", "   ", "<html>"])
def test_unparseable_output_is_a_failure(stdout):
    outcome = parse_critic_output(stdout, EXIT_FINDINGS)

    assert outcome.ok is False
    assert outcome.findings == ()


def test_findings_exit_with_empty_output_is_a_failure():
    outcome = parse_critic_output("", EXIT_FINDINGS)

    assert outcome.ok is False
    assert "output_empty" in outcome.reasons


@pytest.mark.parametrize(
    "payload", [{"error": "boom"}, {"nope": 1}, 7, "plain string", True]
)
def test_an_unexpected_shape_is_a_failure(payload):
    outcome = parse_critic_output(json.dumps(payload), EXIT_FINDINGS)

    assert outcome.ok is False
    assert "output_unexpected" in outcome.reasons


def test_a_failed_scan_outranks_a_payload_mismatch():
    """Exit 1 with an unparseable body is a scan failure, not a shape problem."""
    outcome = parse_critic_output("garbage", EXIT_SCAN_FAILED)

    assert outcome.reasons == ("scan_failed",)


# ---------------------------------------------------------------------------
# Nothing is coerced into a finding the engine did not make
# ---------------------------------------------------------------------------


def test_a_finding_without_a_rule_identity_is_dropped():
    payload = {"findings": [{"finding": "Something is off."}]}

    outcome = parse_critic_output(json.dumps(payload), EXIT_FINDINGS)

    assert outcome.findings == ()


def test_a_finding_without_a_description_is_dropped():
    payload = {"findings": [{"rule_id": "contrast/low"}]}

    outcome = parse_critic_output(json.dumps(payload), EXIT_FINDINGS)

    assert outcome.findings == ()


@pytest.mark.parametrize("entry", ["a string", 7, None, [], [1, 2]])
def test_a_non_mapping_entry_is_dropped(entry):
    outcome = parse_critic_output(
        json.dumps({"findings": [entry, *REAL_FINDINGS["findings"]]}), EXIT_FINDINGS
    )

    assert len(outcome.findings) == 2


def test_an_unknown_severity_is_preserved_not_coerced():
    """Upstream may add a level; hiding that would misreport its urgency."""
    payload = {"findings": [{"rule_id": "r", "finding": "f", "severity": "apocalyptic"}]}

    outcome = parse_critic_output(json.dumps(payload), EXIT_FINDINGS)

    assert outcome.findings[0].severity == "apocalyptic"


def test_a_missing_severity_does_not_become_a_specific_level():
    payload = {"findings": [{"rule_id": "r", "finding": "f"}]}

    outcome = parse_critic_output(json.dumps(payload), EXIT_FINDINGS)

    assert outcome.findings[0].severity == ""


def test_a_missing_category_becomes_the_general_bucket():
    payload = {"findings": [{"rule_id": "r", "finding": "f"}]}

    outcome = parse_critic_output(json.dumps(payload), EXIT_FINDINGS)

    assert outcome.findings[0].category == "general"


# ---------------------------------------------------------------------------
# Output is bounded
# ---------------------------------------------------------------------------


def test_finding_count_is_bounded_and_the_truncation_is_reported():
    payload = {
        "findings": [{"rule_id": f"r{i}", "finding": "f"} for i in range(MAX_FINDINGS + 25)]
    }

    outcome = parse_critic_output(json.dumps(payload), EXIT_FINDINGS)

    assert len(outcome.findings) == MAX_FINDINGS
    assert outcome.truncated is True
    assert "truncated" in outcome.reasons


def test_an_untruncated_result_does_not_claim_truncation():
    outcome = parse_critic_output(json.dumps(REAL_FINDINGS), EXIT_FINDINGS)

    assert outcome.truncated is False
    assert outcome.reasons == ()


def test_field_text_is_bounded():
    payload = {"findings": [{"rule_id": "r", "finding": "x" * 5000}]}

    outcome = parse_critic_output(json.dumps(payload), EXIT_FINDINGS)

    assert len(outcome.findings[0].finding) <= 400


def test_the_outcome_is_serializable():
    outcome = parse_critic_output(json.dumps(REAL_FINDINGS), EXIT_FINDINGS)

    payload = outcome.to_dict()

    assert payload["ok"] is True
    assert len(payload["findings"]) == 2


def test_an_unknown_reason_is_rejected():
    with pytest.raises(ValueError):
        CriticOutcome(ok=False, reasons=("totally_made_up",))


def test_the_reason_vocabulary_is_closed():
    assert set(CRITIC_REASONS) >= {"scan_failed", "engine_unavailable"}


# ---------------------------------------------------------------------------
# Engine resolution is contained and honest
# ---------------------------------------------------------------------------


def test_the_real_verified_layout_resolves(skill_root):
    """The shipped detector resolves, and stays inside the skill root."""
    resolved = resolve_engine_path(skill_root)

    assert resolved is not None
    assert resolved.name == "detect.mjs"
    assert is_contained(skill_root, resolved)


def test_a_missing_engine_resolves_to_nothing(skill_root):
    (skill_root / "scripts" / "detect.mjs").unlink()

    assert resolve_engine_path(skill_root) is None


def test_an_empty_engine_file_is_not_an_engine(skill_root):
    (skill_root / "scripts" / "detect.mjs").write_text("", encoding="utf-8")

    assert resolve_engine_path(skill_root) is None


def test_a_directory_is_not_an_engine(skill_root):
    (skill_root / "scripts" / "detect.mjs").unlink()
    (skill_root / "scripts" / "detect.mjs").mkdir()

    assert resolve_engine_path(skill_root) is None


def test_a_skill_root_that_does_not_exist_resolves_to_nothing(tmp_path):
    assert resolve_engine_path(tmp_path / "nope") is None


def test_containment_is_measured_against_the_resolved_root(skill_root):
    """Containment holds, expressed against resolved paths (no symlink needed)."""
    resolved = skill_root.resolve()

    engine = resolve_engine_path(skill_root)

    assert engine is not None
    assert engine.resolve().is_relative_to(resolved)


def test_containment_is_measured_against_the_resolved_root(skill_root):
    """Containment holds, expressed against resolved paths (no symlink needed)."""
    resolved = skill_root.resolve()

    engine = resolve_engine_path(skill_root)

    assert engine is not None
    assert engine.resolve().is_relative_to(resolved)


def test_a_complete_engine_chain_outside_the_root_is_refused(tmp_path):
    """The refused half of containment, proven without a privileged operation.

    A complete, real engine chain that IS inside its own root resolves; the same
    shape reached from a root that does not contain it does not. A containment
    check implemented as a string prefix, or omitted entirely, would accept both
    and fail here.
    """
    complete = tmp_path / "complete"
    (complete / "scripts" / "detector").mkdir(parents=True)
    (complete / "scripts" / "detect.mjs").write_text("// a\n", encoding="utf-8")
    (complete / "scripts" / "detector" / "detect-antipatterns.mjs").write_text(
        "// b\n", encoding="utf-8"
    )

    # Sanity: the same layout resolves when it really is inside its own root.
    engine = resolve_engine_path(complete)
    assert engine is not None
    assert engine.resolve().is_relative_to(complete.resolve())

    # Now hand the resolver a root that does NOT contain that chain. The files
    # are real and non-empty, so only containment can refuse.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_engine_path(empty) is None



    """Linking a shared skill INTO a profile is allowed, not an escape.

    Containment is measured against the RESOLVED skill root, never a literal
    string prefix, so a root that is itself a link still verifies. Proving the
    allowed half matters as much as the refused half: a containment check
    implemented as a string prefix would wrongly refuse this.
    """
    linked = tmp_path / "shared-skill"
    (linked / "scripts" / "detector").mkdir(parents=True)
    (linked / "scripts" / "detect.mjs").write_text("// a\n", encoding="utf-8")
    (linked / "scripts" / "detector" / "detect-antipatterns.mjs").write_text(
        "// b\n", encoding="utf-8"
    )

    alias = tmp_path / "alias"
    try:
        alias.symlink_to(linked, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a directory symlink without elevation")

    assert resolve_engine_path(alias) is not None


def test_containment_rejects_an_engine_resolving_outside_the_root(skill_root, monkeypatch):
    """The REFUSED half of containment, proven on every host.

    The engine chain is real, non-empty and inside the skill root as the caller
    sees it -- but its RESOLVED location is outside, which is exactly the
    symlink-escape case. Containment is decided on the resolved path, so
    controlling that path is a faithful, portable way to drive the branch; a real
    symlink would additionally require elevation on Windows.
    """
    real_resolve = Path.resolve
    outside = skill_root.parent / "outside-tree"
    outside.mkdir(exist_ok=True)

    def fake_resolve(self, *args, **kwargs):
        # Only the ENTRYPOINT resolves elsewhere; the root and the facade stay
        # put, so the loop reaches containment with a path outside the root.
        if self.name == "detect.mjs":
            return outside / "detect.mjs"
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    assert resolve_engine_path(skill_root) is None


def test_an_engine_escaping_the_skill_is_refused(skill_root):
    """Containment is the load-bearing check, not mere existence.

    The entrypoint is REAL and lives outside the skill root via a symlink, so
    only the containment check can refuse it -- a test pointing at a missing
    path would pass on the ``is_file`` guard and prove nothing here.
    """
    outside = skill_root.parent / "outside.mjs"
    outside.write_text("// planted", encoding="utf-8")
    engine = skill_root / "scripts" / "detect.mjs"
    engine.unlink()

    try:
        engine.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a symlink without elevation")

    assert resolve_engine_path(skill_root) is None


def test_an_unresolved_engine_is_reported_not_silently_clean():
    outcome = run_critic_scan(None, runner=RecordingRunner(stdout="{}"))

    assert outcome.ok is False
    assert outcome.findings == ()
    assert outcome.reasons == ("engine_unavailable",)


def test_an_unresolved_engine_never_runs_a_command():
    runner = RecordingRunner(stdout="{}")

    run_critic_scan(None, runner=runner)

    assert runner.calls == []


def test_normalize_finding_returns_none_for_junk():
    assert normalize_finding("nope") is None
    assert normalize_finding(None) is None
    assert normalize_finding({}) is None


def test_a_real_finding_name_becomes_a_readable_prefix():
    """The shipped `name` is human-readable and must not be dropped."""
    outcome = normalize_finding(
        {"antipattern": "a", "name": "Overused font", "description": "d"}
    )

    assert outcome.finding == "Overused font: d"


def test_alias_keys_are_tolerated_for_the_same_concept():
    """Upstream has used more than one key; a closed alias set handles that."""
    outcome = normalize_finding(
        {"id": "r", "message": "m", "level": "critical", "fix": "do it"}
    )

    assert outcome.rule_id == "r"
    assert "m" in outcome.finding
    assert outcome.severity == "critical"
    assert outcome.suggested_action == "do it"