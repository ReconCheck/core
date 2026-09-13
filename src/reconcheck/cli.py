"""Command line interface: ``reconcheck compare LEFT RIGHT [options]``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .comparison import compare_files
from .errors import ReconCheckError
from .report import write_json


def _parse_normalize(specs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in specs:
        if ":" in spec:
            col, kind = spec.split(":", 1)
            out[col] = kind
    return out


def run_compare(args: argparse.Namespace) -> int:
    try:
        report, _findings = compare_files(
            args.left,
            args.right,
            rules=args.rules,
            match_on=args.match_on,
            normalize=_parse_normalize(args.normalize),
            sheet=args.sheet,
        )
    except (ReconCheckError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if args.output:
        write_json(report, args.output)
        print(f"wrote {args.output}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = report["summary"]
    line = (
        f"summary: {summary['total']} finding(s) "
        f"[{summary['high']} high / {summary['medium']} medium / {summary['low']} low] "
        f"on {summary['aligned_rows']} aligned row(s)"
    )
    print(line, file=sys.stderr if args.output else sys.stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reconcheck",
        description="Cross-document verification: tell you what doesn't match, "
        "by how much, and where in the original file.",
    )
    parser.add_argument("--version", action="version", version=f"reconcheck {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    cmp = sub.add_parser("compare", help="compare two documents")
    cmp.add_argument("left", help="left document (CSV/TSV/XLSX)")
    cmp.add_argument("right", help="right document (CSV/TSV/XLSX)")
    cmp.add_argument(
        "-o", "--output", type=Path, default=None, help="write JSON report here (default: stdout)"
    )
    cmp.add_argument(
        "--rules",
        default=None,
        help="YAML rule file or directory of *.yaml rules (default: built-in auto rule)",
    )
    cmp.add_argument(
        "--match-on",
        nargs="+",
        default=None,
        help="column header(s) to match rows across documents (default: first common header)",
    )
    cmp.add_argument(
        "--normalize",
        action="append",
        default=[],
        metavar="COL:KIND",
        help="normalise a match column (part_no|entity|raw); repeatable",
    )
    cmp.add_argument("--sheet", default=None, help="for XLSX: compare only this sheet")
    cmp.set_defaults(func=run_compare)

    args = parser.parse_args(argv)
    return args.func(args)
