"""Interface reservation for optional LLM enhancement.

The engine stays deterministic by design: rules are the primary judge. An
optional :class:`LLMEnhancer` may assist at two seams (opt-in per job, see
DESIGN.md "LLM participation"):

1. **Alignment** — entity/row disambiguation for rows whose exact match keys
   do not collide (``华加`` vs ``深圳市华加生物科技有限公司``).
2. **Narration** — a natural-language explanation attached to findings.

Rules of thumb: OpenAI-compatible endpoints only (base_url + api_key + model,
operator-configured); documents only travel to an endpoint the operator chose;
every failure degrades to the deterministic result; findings touched by the
model are flagged ``llm_augmented`` in the report.

The concrete OpenAI-compatible client is intentionally not implemented yet —
this module only pins the contract.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMEnhancer(Protocol):
    """The two seams where a model may help."""

    def disambiguate(
        self, left: Any, right: Any, context: dict[str, Any]
    ) -> list[tuple[int, int] | None]:
        """Map unmatched rows of ``left`` to candidates in ``right``.

        Return ``(left_row_index, right_row_index)`` pairs for rows the model
        judged equivalent; callers keep full control over acceptance
        (confidence threshold, limits). Empty list means "no opinion".
        """
        ...

    def explain(self, finding: Any, report: dict[str, Any] | None = None) -> str | None:
        """Human-language explanation for one finding; ``None`` to skip."""
        ...


class NullEnhancer:
    """Default: deterministic engine only, zero network calls."""

    def disambiguate(
        self, left: Any, right: Any, context: dict[str, Any]
    ) -> list[tuple[int, int] | None]:
        return []

    def explain(self, finding: Any, report: dict[str, Any] | None = None) -> str | None:
        return None
