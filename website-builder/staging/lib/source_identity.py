#!/usr/bin/env python3
"""Authoritative staging source-identity contract for Website Builder R2.

WHY THIS EXISTS
---------------
The staging runtime executes whatever Python package tree sits at the launcher's
``SRC`` directory (``/home/wbe2e/src-r2``). A live incident proved that the
``PINNED_COMMIT`` marker file alone is NOT sufficient: the marker named the new
commit while the executable tree was an OLDER commit, so the runtime silently
ran un-pinned code (empty DesignContextPack, spurious N6-E QA parity failures).

This module defines ONE contract, shared by the refresh procedure and the
launcher's fail-closed preflight:

  * ``manifest``        -- snapshot a source tree (commit + content digest).
  * ``verify``          -- assert a live tree matches a manifest (FAIL CLOSED).
  * ``verify-imports``  -- assert the interpreter resolves ``app`` from the
                           pinned source root, never a stale copy.
  * ``verify-runtime``  -- (post-launch) assert a running PID's cwd/cmdline/env
                           point at the pinned source root.

The content digest is deterministic (path-sorted, content-hashed) and ignores
volatile build caches, so it is stable across machines and runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
PACKAGE_SUBDIR = "website-builder"

# Directories never part of the runtime source identity.
EXCLUDE_DIRS = {
    "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".git", ".venv", "venv", ".tox", ".eggs",
}
# Relative prefixes (under website-builder/) excluded from the digest.
EXCLUDE_REL_PREFIXES = ("staging/evidence",)

# Executable modules whose identity matters most; recorded explicitly.
KEY_MODULES = (
    "app/runtime.py",
    "app/projects/build.py",
    "app/core/design_context.py",
    "app/core/design_selection.py",
    "app/core/design_retrieval.py",
    "app/qa/orchestrator.py",
)

# Modules probed by ``verify-imports``.
IMPORT_PROBE_MODULES = (
    "app",
    "app.runtime",
    "app.projects.build",
    "app.core.design_context",
    "app.qa.orchestrator",
)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _excluded(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in EXCLUDE_REL_PREFIXES)


def _iter_entries(package_root: Path):
    """Yield (relpath, kind, content) for every identity-bearing entry.

    Symlinks are recorded by their target (never followed), so swapping a real
    file for a symlink changes the digest and is caught.
    """
    for dirpath, dirnames, filenames in os.walk(package_root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            try:
                rel = p.relative_to(package_root).as_posix()
            except ValueError:
                continue
            if _excluded(rel):
                continue
            if p.is_symlink():
                yield rel, "link", os.readlink(p).encode("utf-8", "surrogateescape")
            elif p.is_file():
                yield rel, "file", p.read_bytes()


def tree_digest(package_root: Path):
    h = hashlib.sha256()
    n_files = n_links = 0
    for rel, kind, content in sorted(_iter_entries(package_root)):
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        h.update(kind.encode())
        h.update(b"\0")
        h.update(_sha256_hex(content).encode())
        h.update(b"\n")
        if kind == "link":
            n_links += 1
        else:
            n_files += 1
    return h.hexdigest(), n_files, n_links


def build_manifest(src: Path, commit: str) -> dict:
    src = Path(src).resolve()
    package_root = src / PACKAGE_SUBDIR
    if not package_root.is_dir():
        raise SystemExit(f"REFUSING: {package_root} is not a directory")
    digest, n_files, n_links = tree_digest(package_root)
    modules = {}
    for rel in KEY_MODULES:
        p = package_root / rel
        modules[rel] = (
            _sha256_hex(p.read_bytes())
            if p.is_file() and not p.is_symlink()
            else None
        )
    return {
        "schema": SCHEMA,
        "commit": commit,
        "source_root": str(src),
        "package_root": str(package_root),
        "tree_digest": digest,
        "file_count": n_files,
        "symlink_count": n_links,
        "key_modules": modules,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def verify_manifest(src: Path, manifest_path: Path, expect_commit=None):
    errors = []
    if not manifest_path.is_file():
        return [f"missing source-identity manifest: {manifest_path}"]
    try:
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"unreadable manifest {manifest_path}: {exc}"]
    if man.get("schema") != SCHEMA:
        errors.append(f"manifest schema {man.get('schema')} != {SCHEMA}")
    src = Path(src).resolve()
    if str(man.get("source_root")) != str(src):
        errors.append(f"manifest source_root {man.get('source_root')} != live {src}")
    if expect_commit and man.get("commit") != expect_commit:
        errors.append(
            f"manifest commit {man.get('commit')} != pinned {expect_commit}"
        )
    package_root = src / PACKAGE_SUBDIR
    if not package_root.is_dir():
        errors.append(f"{package_root} is not a directory")
        return errors
    digest, n_files, _n_links = tree_digest(package_root)
    if digest != man.get("tree_digest"):
        errors.append(
            f"tree digest mismatch: live {digest} != manifest {man.get('tree_digest')}"
        )
    if n_files != man.get("file_count"):
        errors.append(
            f"file count mismatch: live {n_files} != manifest {man.get('file_count')}"
        )
    for rel, want in (man.get("key_modules") or {}).items():
        p = package_root / rel
        got = (
            _sha256_hex(p.read_bytes())
            if p.is_file() and not p.is_symlink()
            else None
        )
        if got != want:
            errors.append(f"module {rel} digest mismatch: live {got} != manifest {want}")
    return errors


def verify_imports(src: Path, modules=IMPORT_PROBE_MODULES):
    """Assert the interpreter resolves the probed modules from ``src`` only."""
    src = Path(src).resolve()
    package_root = src / PACKAGE_SUBDIR
    if not package_root.is_dir():
        return [f"{package_root} is not a directory"]
    program = (
        "import importlib.util as u, json, sys\n"
        "out = {}\n"
        "for m in sys.argv[1:]:\n"
        "    s = u.find_spec(m)\n"
        "    out[m] = s.origin if s else None\n"
        "print(json.dumps(out))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src)
    proc = subprocess.run(
        [sys.executable, "-c", program, *modules],
        cwd=str(package_root),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return [f"import probe failed: {proc.stderr.strip() or proc.returncode}"]
    try:
        found = json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        return [f"import probe produced unparsable output: {exc}: {proc.stdout!r}"]
    errors = []
    for mod, origin in found.items():
        if not origin:
            errors.append(f"module {mod} not importable from {src}")
            continue
        try:
            Path(origin).resolve().relative_to(src)
        except ValueError:
            errors.append(f"module {mod} resolves OUTSIDE pinned source: {origin}")
    return errors


def verify_runtime(proc_root: Path, pid: int, expect_cwd=None, expect_src=None):
    """Assert a running PID's cwd/cmdline/PYTHONPATH point at the pinned source."""
    errors = []
    base = Path(proc_root) / str(pid)
    if not base.is_dir():
        return [f"no such process directory: {base}"]
    try:
        cwd = os.readlink(base / "cwd")
    except OSError as exc:
        cwd = None
        errors.append(f"cannot read cwd: {exc}")
    if cwd and expect_cwd and os.path.realpath(cwd) != os.path.realpath(expect_cwd):
        errors.append(f"cwd {cwd} != expected {expect_cwd}")
    try:
        raw = (base / "cmdline").read_bytes()
    except OSError as exc:
        raw = b""
        errors.append(f"cannot read cmdline: {exc}")
    argv = [c.decode("utf-8", "replace") for c in raw.split(b"\0") if c]
    if "-m" not in argv or "app" not in argv:
        errors.append(f"cmdline is not 'python -m app': {argv}")
    try:
        envraw = (base / "environ").read_bytes()
    except OSError:
        envraw = b""
    env = {}
    for kv in envraw.split(b"\0"):
        if b"=" in kv:
            k, _, v = kv.partition(b"=")
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    if expect_src:
        pypath = env.get("PYTHONPATH", "")
        parts = [p for p in pypath.split(os.pathsep) if p]
        if not any(os.path.realpath(p) == os.path.realpath(expect_src) for p in parts):
            errors.append(
                f"process PYTHONPATH {pypath!r} lacks pinned source {expect_src}"
            )
    return errors


def _cmd_manifest(args) -> int:
    man = build_manifest(Path(args.src), args.commit)
    text = json.dumps(man, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"OK: wrote manifest for {man['source_root']} @ {man['commit']}")
        print(f"    tree_digest={man['tree_digest']}")
        print(f"    files={man['file_count']} symlinks={man['symlink_count']}")
    else:
        sys.stdout.write(text)
    return 0


def _cmd_verify(args) -> int:
    errors = verify_manifest(Path(args.src), Path(args.manifest), args.expect_commit)
    if errors:
        print("SOURCE-IDENTITY MISMATCH:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"OK: source tree matches manifest ({args.src})")
    return 0


def _cmd_verify_imports(args) -> int:
    modules = tuple(args.modules) if args.modules else IMPORT_PROBE_MODULES
    errors = verify_imports(Path(args.src), modules)
    if errors:
        print("IMPORT-IDENTITY MISMATCH:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"OK: interpreter resolves app modules from {args.src}")
    return 0


def _cmd_verify_runtime(args) -> int:
    errors = verify_runtime(
        Path(args.proc_root), args.pid, args.expect_cwd, args.expect_src
    )
    if errors:
        print("RUNTIME-IDENTITY MISMATCH:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"OK: pid {args.pid} runs from the pinned source")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("manifest", help="snapshot a source tree")
    m.add_argument("--src", required=True)
    m.add_argument("--commit", required=True)
    m.add_argument("--out")
    m.set_defaults(func=_cmd_manifest)

    v = sub.add_parser("verify", help="assert a live tree matches a manifest")
    v.add_argument("--src", required=True)
    v.add_argument("--manifest", required=True)
    v.add_argument("--expect-commit")
    v.set_defaults(func=_cmd_verify)

    vi = sub.add_parser("verify-imports", help="assert interpreter resolves app from src")
    vi.add_argument("--src", required=True)
    vi.add_argument("--modules", nargs="*")
    vi.set_defaults(func=_cmd_verify_imports)

    vr = sub.add_parser("verify-runtime", help="assert a running PID uses the pinned source")
    vr.add_argument("--proc-root", default="/proc")
    vr.add_argument("--pid", type=int, required=True)
    vr.add_argument("--expect-cwd")
    vr.add_argument("--expect-src")
    vr.set_defaults(func=_cmd_verify_runtime)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
