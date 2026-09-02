#!/usr/bin/env python
"""Prepare the isolated checkout for a run (plan v2 sec.4).

"Runs against an isolated checkout, never a working copy" is a hard constraint,
so it is enforced here rather than trusted: the workspace path is refused if it
sits inside the developer's Code directory. An agent that resets a branch in the
wrong directory destroys uncommitted work.

Usage: python workspace.py --ticket NIK-104   [--print-dir]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from ralph.config import ConfigError, configure_stdio, load

# Directories that must never be used as the agent's workspace.
PROTECTED_ROOTS = [Path("C:/Users/Nikolay/Code")]


class WorkspaceError(RuntimeError):
    pass


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, timeout=600,
    )
    if check and proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    return proc.stdout.strip()


def assert_safe_workspace(path: Path) -> None:
    """Refuse to operate inside a real working copy."""
    resolved = path.resolve()
    for protected in PROTECTED_ROOTS:
        try:
            protected_resolved = protected.resolve()
        except OSError:
            continue
        if resolved == protected_resolved or protected_resolved in resolved.parents:
            raise WorkspaceError(
                f"refusing to use {resolved} as the agent workspace: it is inside "
                f"{protected_resolved}, which holds live working copies. The agent "
                f"resets branches hard; point REPO_DIR somewhere isolated."
            )


def prepare(repo_dir: Path, remote: str, base: str, branch: str) -> dict:
    """Clone or refresh the checkout and put it on a clean branch off `base`."""
    assert_safe_workspace(repo_dir)

    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        git("clone", remote, str(repo_dir))

    actual_remote = git("remote", "get-url", "origin", cwd=repo_dir)
    if actual_remote.rstrip("/").removesuffix(".git") != remote.rstrip("/").removesuffix(".git"):
        raise WorkspaceError(
            f"{repo_dir} points at {actual_remote}, not the configured {remote}"
        )

    git("fetch", "origin", "--prune", cwd=repo_dir)
    # Hard reset is safe *because* of assert_safe_workspace above.
    git("checkout", "-B", branch, f"origin/{base}", cwd=repo_dir)
    git("reset", "--hard", f"origin/{base}", cwd=repo_dir)
    git("clean", "-fdx", "-e", "node_modules", "-e", ".env*", cwd=repo_dir)

    return {
        "dir": str(repo_dir),
        "branch": branch,
        "base": base,
        "head": git("rev-parse", "--short", "HEAD", cwd=repo_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the isolated checkout")
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--config")
    parser.add_argument("--print-dir", action="store_true")
    parser.add_argument("--check-only", action="store_true",
                        help="validate the workspace path without touching git")
    args = parser.parse_args(argv)
    configure_stdio()

    try:
        cfg = load(args.config) if args.config else load()
    except ConfigError as exc:
        print(f"workspace: config error: {exc}", file=sys.stderr)
        return 1

    repo_dir = os.environ.get("REPO_DIR")
    if not repo_dir:
        print("workspace: REPO_DIR is not set", file=sys.stderr)
        return 1

    branch = cfg.branch_for(args.ticket)
    try:
        if args.check_only:
            assert_safe_workspace(Path(repo_dir))
            print(f"workspace: {repo_dir} is an acceptable workspace")
            return 0
        info = prepare(
            Path(repo_dir), cfg.repo["remote"], cfg.repo["default_branch"], branch
        )
    except WorkspaceError as exc:
        print(f"workspace: {exc}", file=sys.stderr)
        return 1

    if args.print_dir:
        print(info["dir"])
    else:
        print(f"workspace: {info['dir']} on {info['branch']} at {info['head']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
