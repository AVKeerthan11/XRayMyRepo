"""The ``xray`` command.

    xray analyze PATH ...   analyze a local directory, print its CIM document
                            (xraymyrepo.analyzer.cli, unchanged)
    xray ingest URL ...     ingest a GitHub commit as a snapshot in PostgreSQL
                            (xraymyrepo.ingestion.cli; needs psycopg)

Only the first argument is parsed here; each command parses the rest. The ingestion
command is imported only when used, so ``xray analyze`` needs no database driver.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

COMMANDS = {
    "analyze": "analyze a local directory and print its CIM document (xray analyze -h)",
    "ingest": "ingest a GitHub repository commit into PostgreSQL (xray ingest -h)",
}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="xray", description="XRayMyRepo")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, summary in COMMANDS.items():
        commands.add_parser(name, help=summary, add_help=False)
    command = parser.parse_args(args[:1]).command  # -h, a missing or unknown command exit here
    if command == "ingest":
        from .ingestion.cli import main as ingest

        return ingest(args[1:])
    from .analyzer.cli import main as analyze

    return analyze(args)
