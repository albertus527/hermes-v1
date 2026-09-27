"""Mutation driver: revert one guard at a time and prove the tests go red.

Not part of the suite. Run from ``website-builder`` with ``py -3 tools/mutation_check.py``.

The mutations are applied to a THROWAWAY COPY of the tree, never to the working
tree. The previous revision of this driver edited the real files and restored
them in a ``finally``; a killed process left four guards reverted and the suite
red for reasons that had nothing to do with what was being tested. Copying is
cheap here (the tree is ~10 MB) and it removes the failure mode entirely: the
only files this tool writes are under a temporary directory it owns.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")

MUTATIONS = [
    (
        "pointer_mode gate",
        "app/sandbox/runner.py",
        """        if token is None:
            if self.pointer_mode(project_id):
                raise PointerResolutionError(
                    WORKSPACE_POINTER_MISSING_AFTER_HYDRATION,
                    "Workspace pointer is missing after hydration")
            return root""",
        """        if token is None:
            return root""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "pointer_mode monotonicity",
        "app/deploy/hydrate.py",
        """    if deployment.get("pointer_mode") is not True:
        deployment["pointer_mode"] = True""",
        """    deployment["pointer_mode"] = deployment.get("pointer_mode", False)""",
        ["tests/test_r2_hydration.py", "tests/test_r2_canonical_source.py"],
    ),
    (
        "malformed-pointer refusal",
        "app/sandbox/runner.py",
        """        if _SHA1_RE.fullmatch(token):
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID,
                "Workspace pointer must not hold a commit id")
        return validate_operation_token(token)""",
        """        return token""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "pointer-target preservation",
        "app/sandbox/runner.py",
        """        if token:
            preserved.add(token)""",
        """        preserved.discard(token)""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "foreign-current hold",
        "app/deploy/hydrate.py",
        """            if record.state != HYDRATION_READY and pointer == record.op_token:
                # Never delete or sweep that directory, and never start a new
                # hydration on top of it: the owning operation must finish.
                raise HydrationError(
                    HYDRATION_RECOVERY_REQUIRED,
                    "another operation owns the current workspace",
                    operation_id=record.operation_id)""",
        """            if False:
                raise HydrationError(
                    HYDRATION_RECOVERY_REQUIRED,
                    "another operation owns the current workspace",
                    operation_id=record.operation_id)""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "foreign-READY supersede (D24)",
        "app/deploy/hydrate.py",
        """            if record.state != HYDRATION_READY and pointer == record.op_token:""",
        """            if pointer == record.op_token:""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "base immutability",
        "app/deploy/hydrate.py",
        """        if other.to_dict() != self.to_dict():
            raise HydrationError(
                HYDRATION_BASE_DRIFT, "the reserved revision base changed")""",
        """        if False:
            raise HydrationError(
                HYDRATION_BASE_DRIFT, "the reserved revision base changed")""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "canonical-source refusal",
        "app/projects/revise.py",
        """                if not verdict.ready:
                    raise CanonicalSourceRefusal(
                        verdict.error_code or "CANONICAL_SOURCE_COMMIT_MISSING")""",
        """                if not verdict.ready:
                    return build_live_base(
                        state.deployment["last_live_release"],
                        seq=seq, reserved_at=time.time(),
                        requirements_version=state.revisions.requirements_version,
                        design_dna_version=state.revisions.design_dna_version)""",
        ["tests/test_r2_canonical_source.py"],
    ),
    (
        "publication_parent root semantics",
        "app/projects/release.py",
        """    if parent is not None and not _is_sha1(parent):""",
        """    if not _is_sha1(parent):""",
        ["tests/test_r2_canonical_source.py"],
    ),
    (
        "repository identity gate",
        "app/deploy/hydrate.py",
        """        try:
            verify_repository_identity(self.source_repo_url, base.publication_repo)
        except MaterializationRefusal as exc:
            raise HydrationError(HYDRATION_REPO_MISMATCH, exc.reason) from exc""",
        """        try:
            verify_repository_identity(self.source_repo_url, base.publication_repo)
        except MaterializationRefusal as exc:
            if False:
                raise HydrationError(HYDRATION_REPO_MISMATCH, exc.reason) from exc""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "ls-tree mode gate",
        "app/deploy/git_output.py",
        """    if mode == '120000':
        raise MaterializationRefusal('symlink', path)
    if mode == '160000':
        raise MaterializationRefusal('submodule', path)
    if mode not in _ACCEPTED_BLOB_MODES:
        raise MaterializationRefusal('mode', path)
    if kind != 'blob':
        raise MaterializationRefusal('non_blob', path)""",
        """    if False:
        raise MaterializationRefusal('symlink', path)""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "unsafe path predicate",
        "app/deploy/git_output.py",
        """    if unsafe_repository_path(path):
        raise MaterializationRefusal('unsafe_path', path)""",
        """    if False:
        raise MaterializationRefusal('unsafe_path', path)""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "containment component walk",
        "app/deploy/git_output.py",
        """    current = staging
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise MaterializationRefusal('containment')""",
        """    current = staging
    for part in relative.parts:
        current = current / part""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "resolved containment check",
        "app/deploy/git_output.py",
        """    if resolved != root and root not in resolved.parents:
        raise MaterializationRefusal('containment')""",
        """    if False:
        raise MaterializationRefusal('containment')""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "hook neutralisation during hydration",
        "app/deploy/git_output.py",
        """    command = ['git', '-c', 'core.longpaths=true', '-c', 'core.hooksPath=' + os.devnull]""",
        """    command = ['git', '-c', 'core.longpaths=true']""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "canonical LFS detection",
        "app/deploy/git_output.py",
        """            if looks_like_canonical_lfs_pointer(data):
                raise MaterializationRefusal('lfs', path)""",
        """            if False:
                raise MaterializationRefusal('lfs', path)""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "read_tree re-walk",
        "app/deploy/hydrate.py",
        """        try:
            source = read_tree(op_dir, EXCLUDED)
            dist = read_tree(Path(op_dir) / "dist")
        except (ValueError, OSError) as exc:
            raise HydrationError(
                HYDRATION_UNSAFE_ENTRY,
                "the hydrated workspace is not readable") from exc""",
        """        source, dist = {}, {}""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "name-set recheck",
        "app/deploy/hydrate.py",
        """        if expected_source_names is not None and set(source) != set(expected_source_names):
            raise HydrationError(
                HYDRATION_UNSAFE_ENTRY,
                "the hydrated source names are not the committed names")
        if expected_dist_names is not None and set(dist) != set(expected_dist_names):
            raise HydrationError(
                HYDRATION_UNSAFE_ENTRY,
                "the hydrated artifact names are not the committed names")""",
        """        if False:
            raise HydrationError(
                HYDRATION_UNSAFE_ENTRY,
                "the hydrated source names are not the committed names")""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "per-kind hydration record validation",
        "app/deploy/hydrate.py",
        """            for name in _RECORD_LIVE_IDENTITY:
                if getattr(self, name) is not None:
                    raise HydrationError(
                        HYDRATION_RECORD_INVALID,
                        f"a DRAFT record must carry {name} as null")""",
        """            for name in _RECORD_LIVE_IDENTITY:
                if False:
                    raise HydrationError(
                        HYDRATION_RECORD_INVALID,
                        f"a DRAFT record must carry {name} as null")""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "D16 no-delete / no-rollback",
        "app/deploy/hydrate.py",
        """        self.runner.write_pointer(project_id, op_token)
        ready = self._commit_ready(project_id, verified)""",
        """        try:
            self.runner.write_pointer(project_id, op_token)
            ready = self._commit_ready(project_id, verified)
        except Exception:
            shutil.rmtree(Path(verified.op_dir), ignore_errors=True)
            raise""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "D16 does not transition FAILED",
        "app/projects/revise.py",
        """                    if exc.error_code == HYDRATION_STATE_UNPERSISTED:""",
        """                    if False:""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "D25 persisted error_code",
        "app/projects/revise.py",
        """                "error": error,
                "error_code": error_code,
                "failed_at": time.time(),""",
        """                "error": error,
                "failed_at": time.time(),""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "resolve-before-mkdir runtime boundary",
        "app/sandbox/runner.py",
        """        path = self.resolve_workspace(project_id)
        path.mkdir(parents=True, exist_ok=True)

        # Create subdirectories
        for name in RUNTIME_DIRNAMES:
            (path / name).mkdir(exist_ok=True)""",
        """        path = project_workspace_path(self.workspace_root, project_id)
        path.mkdir(parents=True, exist_ok=True)

        # Create subdirectories
        for name in RUNTIME_DIRNAMES:
            (path / name).mkdir(exist_ok=True)""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "D8 legacy_source_sync cleanup",
        "app/projects/release.py",
        """            state.deployment.pop("legacy_source_sync", None)""",
        """            pass""",
        ["tests/test_r2_canonical_source.py"],
    ),
    (
        "D8' migration projection",
        "app/core/state.py",
        """    sync_status = repository.get("sync_status")
    if isinstance(sync_status, str) and sync_status:""",
        """    sync_status = None
    if isinstance(sync_status, str) and sync_status:""",
        ["tests/test_r2_canonical_source.py"],
    ),
    (
        "tested/publication tree agreement",
        "app/deploy/hydrate.py",
        """        if base.tested_tree != base.publication_tree:
            raise HydrationError(
                HYDRATION_TREE_MISMATCH,
                "the recorded tested and publication trees differ")""",
        """        if False:
            raise HydrationError(
                HYDRATION_TREE_MISMATCH,
                "the recorded tested and publication trees differ")""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "publication_commit is the source, never tested_commit",
        "app/deploy/hydrate.py",
        """        if not repo.has_commit(base.publication_commit):
            try:""",
        """        if False:
            try:""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "verified staging is re-verified before the swap",
        "app/deploy/hydrate.py",
        """        try:
            self._verify(op_dir, expected_source=base.source_sha256,
                         expected_artifact=base.artifact_sha256)
        except HydrationError:
            return None""",
        """        if op_dir.is_symlink() or not op_dir.is_dir():
            return None""",
        ["tests/test_r2_hydration.py"],
    ),
    (
        "staging is disposable under our own op name",
        "app/deploy/hydrate.py",
        """        if op_dir.exists():
            shutil.rmtree(op_dir, ignore_errors=True)""",
        """        if op_dir.exists():
            raise HydrationError(
                HYDRATION_STAGING_UNAVAILABLE, "staging already exists")""",
        ["tests/test_r2_hydration.py"],
    ),
]


def build_sandbox(destination: Path) -> Path:
    for entry in ROOT.iterdir():
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, ignore=IGNORED)
        else:
            shutil.copy2(entry, target)
    return destination


def main() -> int:
    # One sandbox for the whole run. Every mutation is applied by overwriting
    # exactly one file in it, and the pristine copy is re-written afterwards, so
    # a mutation can never leak into the next one or into the working tree.
    with tempfile.TemporaryDirectory(prefix="wb-mutation-") as tmp:
        sandbox = build_sandbox(Path(tmp) / ROOT.name)
        results = []
        for name, relative, before, after, tests in MUTATIONS:
            source = (ROOT / relative).read_text(encoding="utf-8")
            if before not in source:
                results.append((name, "ANCHOR-NOT-FOUND", ""))
                print(f"!! {name}: anchor not found in {relative}", flush=True)
                continue
            target = sandbox / relative
            target.write_text(source.replace(before, after, 1), encoding="utf-8")
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
                     "--tb=no", *tests],
                    cwd=sandbox, capture_output=True, text=True)
                code, out = proc.returncode, proc.stdout
            finally:
                target.write_text(source, encoding="utf-8")
            tail = out.strip().splitlines()[-1] if out.strip() else ""
            results.append((name, "KILLED" if code else "SURVIVED", tail))
            print(f"{'KILLED  ' if code else 'SURVIVED'} {name}", flush=True)

        drifted = [relative for _, relative, _, _, _ in MUTATIONS
                   if (sandbox / relative).read_text(encoding="utf-8")
                   != (ROOT / relative).read_text(encoding="utf-8")]

    print()
    survivors = [name for name, status, _ in results if status != "KILLED"]
    print(f"{len(results) - len(survivors)}/{len(results)} killed")
    for name in survivors:
        print(f"  NOT PROVEN: {name}")
    if drifted:
        print(f"  SANDBOX DRIFT: {drifted}")
    return 1 if survivors or drifted else 0


if __name__ == "__main__":
    sys.exit(main())
