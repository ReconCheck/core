"""Command line interface: ``reconcheck compare LEFT RIGHT [options]``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .comparison import compare_files, compare_three
from .errors import ReconCheckError
from .parse import load_document
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


def run_compare3(args: argparse.Namespace) -> int:
    try:
        docs = [load_document(path, sheet=args.sheet) for path in args.documents]
        report = compare_three(
            docs,
            rules=args.rules,
            match_on=args.match_on,
            normalize=_parse_normalize(args.normalize),
        )
    except (ReconCheckError, OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if args.output:
        write_json(report, args.output)
        print(f"wrote {args.output}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = report["summary"]
    line = (
        f"summary: pairs={summary['pairs_evaluated']} "
        f"pair_findings={summary['pair_findings_total']} conflicts={summary['conflict_total']} "
        f"({summary['conflict_high']} high / {summary['conflict_medium']} medium) "
        f"aligned={summary['aligned_rows']}"
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

    c3 = sub.add_parser(
        "compare3",
        help="three-way verification: purchase order / delivery note / invoice",
    )
    c3.add_argument("documents", nargs=3, metavar="DOC", help="three documents (CSV/TSV/XLSX)")
    c3.add_argument(
        "-o", "--output", type=Path, default=None, help="write JSON report here (default: stdout)"
    )
    c3.add_argument(
        "--rules",
        default=None,
        help="YAML rule file or directory of *.yaml rules (default: built-in auto rule)",
    )
    c3.add_argument(
        "--match-on",
        nargs="+",
        default=None,
        help="column header(s) to match rows across documents (default: first common header)",
    )
    c3.add_argument(
        "--normalize",
        action="append",
        default=[],
        metavar="COL:KIND",
        help="normalise a match column (part_no|entity|raw); repeatable",
    )
    c3.add_argument("--sheet", default=None, help="for XLSX: compare only this sheet")
    c3.set_defaults(func=run_compare3)

    args = parser.parse_args(argv)
    return args.func(args)
