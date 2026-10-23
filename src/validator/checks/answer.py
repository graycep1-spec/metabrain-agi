"""Профиль Answer: grounding и pii_dlp (C-4)."""

from collections.abc import Mapping
from typing import Any

from src.validator.checks.detectors import ALWAYS_FORBIDDEN, detect
from src.validator.scorecard import Context
from src.validator.service import CheckOutcome


def grounding(threshold: float = 0.0):
    """Каждый claim ссылается на существующий источник; доля непокрытых ≤ threshold."""

    def check(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
        claims = artifact.get("claims")
        sources = artifact.get("sources")
        if not isinstance(claims, list) or not isinstance(sources, list):
            return CheckOutcome("fail", {"reason": "malformed"}, "claims and sources must be lists")
        if not claims:
            return CheckOutcome("fail", {"reason": "no_claims", "claims": 0}, "answer has no claims to verify")

        problems: list[str] = []
        known: set[str] = set()
        malformed_sources = 0
        for index, source in enumerate(sources):
            if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
                malformed_sources += 1
                problems.append(f"sources[{index}] has no source_id")
                continue
            missing = [f for f in ("system", "object_ref", "read_at") if not source.get(f)]
            if missing:
                malformed_sources += 1
                problems.append(f"source {source['source_id']} misses {missing}")
            known.add(source["source_id"])

        uncovered = 0
        unknown_refs = 0
        for index, claim in enumerate(claims):
            refs = claim.get("source_ids") if isinstance(claim, Mapping) else None
            if not isinstance(refs, list) or not refs:
                uncovered += 1
                problems.append(f"claims[{index}] has no source_ids")
                continue
            dangling = [ref for ref in refs if ref not in known]
            if dangling:
                uncovered += 1
                unknown_refs += len(dangling)
                problems.append(f"claims[{index}] cites unknown sources {dangling}")

        ratio = uncovered / len(claims)
        metrics = {
            "claims": len(claims),
            "uncovered": uncovered,
            "uncovered_ratio": round(ratio, 4),
            "unknown_source_refs": unknown_refs,
            "malformed_sources": malformed_sources,
            "threshold": threshold,
        }
        ok = ratio <= threshold and unknown_refs == 0 and malformed_sources == 0
        return CheckOutcome("pass" if ok else "fail", metrics, "\n".join(problems))

    return check


def _answer_text(artifact: Mapping[str, Any]) -> str:
    parts = [str(artifact.get("text", ""))]
    for claim in artifact.get("claims") or []:
        if isinstance(claim, Mapping):
            parts.append(str(claim.get("text", "")))
    return "\n".join(parts)


def pii_dlp(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    """Ответ владельцу харнесса: контакты клиента допустимы, остальные категории — нет."""
    found = detect(_answer_text(artifact))
    forbidden = {name: count for name, count in found.items() if name in ALWAYS_FORBIDDEN}
    metrics = {"found": found, "forbidden": sorted(forbidden)}
    return CheckOutcome("fail" if forbidden else "pass", metrics)
