"""Minimal command line: ``xray analyze PATH`` prints the canonical CIM JSON.

Exit codes: 0 success, 1 usage or input error, 2 the analyzer produced an invalid
CIM document (a bug; the contract violation is printed).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from xraymyrepo.cim import RepositoryRef

from .build import AnalyzerContractError
from .pipeline import analyze

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xray", description="XRayMyRepo analyzer")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("analyze", help="analyze a repository and print its CIM document")
    run.add_argument("path", type=Path)
    run.add_argument("--commit", help="commit SHA to record (default: git HEAD of PATH)")
    run.add_argument(
        "--repository",
        metavar="HOST/OWNER/NAME",
        help="repository identity (default: local/local/<directory name>)",
    )
    run.add_argument("-o", "--output", type=Path, help="write the document here instead of stdout")
    args = parser.parse_args(argv)

    root: Path = args.path
    if not root.is_dir():
        print(f"xray: not a directory: {root}", file=sys.stderr)
        return 1
    commit = args.commit or _git_head(root)
    if commit is None:
        print("xray: cannot determine the commit; pass --commit", file=sys.stderr)
        return 1
    try:
        repository = _repository(args.repository, root)
        document = analyze(root, repository=repository, commit_sha=commit)
    except AnalyzerContractError as exc:
        print(f"xray: {exc}", file=sys.stderr)
        return 2
    except (ValueError, ValidationError) as exc:
        print(f"xray: {exc}", file=sys.stderr)
        return 1
    text = document.to_canonical_json()
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.write_text(text, encoding="utf-8")
    return 0


def _repository(value: str | None, root: Path) -> RepositoryRef:
    if value is None:
        name = _UNSAFE.sub("-", root.resolve().name).strip("-.") or "repository"
        return RepositoryRef(host="local", owner="local", name=name)
    host, sep1, rest = value.partition("/")
    owner, sep2, name = rest.partition("/")
    if not (sep1 and sep2):
        raise ValueError(f"--repository must be HOST/OWNER/NAME, got {value!r}")
    return RepositoryRef(host=host, owner=owner, name=name)


def _git_head(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


if __name__ == "__main__":
    raise SystemExit(main())
