"""Key normalisation for cross-document alignment.

Full entity resolution is a later milestone; these normalisers handle the
mechanical part — case, separators, leading zeros, legal suffixes.
"""

from __future__ import annotations

import re

_COMPANY_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "集团",
    "公司",
    "corporation",
    "corp.",
    "inc.",
    "incorporated",
    "ltd.",
    "limited",
    "co.",
    "llc",
    "llp",
    "s.a.",
    "gmbh",
    "s.r.l.",
    "srl",
    "b.v.",
    "ag",
)


def normalize_part_no(value: str) -> str:
    """``'0012'``, ``'A-012'``, ``'A 012'``, ``'a012'`` all become ``'a12'``."""
    s = value.strip().lower()
    s = re.sub(r"[\s\-_./:]+", "", s)
    s = re.sub(r"^0+(?=\d)", "", s)          # "0012" -> "12"
    s = re.sub(r"(?<=[a-z])0+(?=\d)", "", s)  # "a012" -> "a12"
    return s


def normalize_entity(value: str) -> str:
    """Lowercase, strip whitespace and one legal suffix.

    ``'深圳市华加生物科技有限公司'`` -> ``'深圳市华加生物科技'``.
    """
    s = value.strip().lower()
    s = re.sub(r"\s+", "", s)
    for suffix in sorted(_COMPANY_SUFFIXES, key=len, reverse=True):
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s


def normalize_key(value: str, kind: str) -> str:
    if kind == "part_no":
        return normalize_part_no(value)
    if kind == "entity":
        return normalize_entity(value)
    return value.strip()
