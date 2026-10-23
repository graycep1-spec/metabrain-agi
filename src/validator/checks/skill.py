"""Профиль Skill: schema, pii_dlp, foreign_data, provenance (C-4, навык — C-5)."""

from collections.abc import Mapping
from typing import Any

from src.validator.checks.detectors import detect
from src.validator.checks.shape import NonEmpty, OneOf, Optional, errors
from src.validator.scorecard import Context
from src.validator.service import CheckOutcome


CANDIDATE = {
    "scope": OneOf("personal", "company"),
    "company_id": NonEmpty(str),
    "owner_harness_id": Optional(str),
    "author_person_id": Optional(str),
    "title": NonEmpty(str),
    "applies_to": {"role_tags": [str], "task_class": NonEmpty(str)},
    "body": NonEmpty(str),
    "examples": ["any"],
    "supersedes": Optional(str),
    "provenance": {
        "source_jobs": [str],
        "scorecard_ref": Optional(str),
        "consent_ref": Optional(str),
        "created_at": Optional(str),
    },
}
SKILL_ARTIFACT = {
    "candidate": CANDIDATE,
    "source_job_summaries": [{"job_id": NonEmpty(str), "identifiers": [str]}],
}
MIN_IDENTIFIER = 3


def _candidate(artifact: Mapping[str, Any]) -> Mapping[str, Any]:
    candidate = artifact.get("candidate")
    return candidate if isinstance(candidate, Mapping) else {}


def _text(candidate: Mapping[str, Any]) -> str:
    examples = candidate.get("examples") or []
    return "\n".join([str(candidate.get("title", "")), str(candidate.get("body", ""))] + [str(e) for e in examples])


def schema(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    found = errors(artifact, SKILL_ARTIFACT)
    candidate = _candidate(artifact)
    for assigned in ("skill_id", "version"):
        if assigned in candidate:
            found.append(f"$.candidate.{assigned}: is assigned on publish, must be absent")
    if candidate.get("company_id") not in (None, str(context.company_id)):
        found.append("$.candidate.company_id: differs from context")
    if candidate.get("scope") == "personal" and candidate.get("owner_harness_id") != str(context.harness_id):
        found.append("$.candidate.owner_harness_id: must be the context harness for a personal skill")
    if candidate.get("scope") == "company" and not candidate.get("author_person_id"):
        found.append("$.candidate.author_person_id: is required for a company skill")
    return CheckOutcome("fail" if found else "pass", {"errors": len(found)}, "\n".join(found))


def pii_dlp(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    """В навыке недопустима ни одна категория, включая контакты."""
    found = detect(_text(_candidate(artifact)))
    return CheckOutcome("fail" if found else "pass", {"found": found})


def foreign_data(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    """Навык компании не несёт идентификаторов из исходных работ автора (C-012)."""
    candidate = _candidate(artifact)
    if candidate.get("scope") != "company":
        return CheckOutcome("pass", {"scope": candidate.get("scope"), "checked": False})
    text = _text(candidate).casefold()
    identifiers = {
        identifier.strip()
        for summary in artifact.get("source_job_summaries") or []
        if isinstance(summary, Mapping)
        for identifier in summary.get("identifiers") or []
        if isinstance(identifier, str) and len(identifier.strip()) >= MIN_IDENTIFIER
    }
    leaked = sorted(i for i in identifiers if i.casefold() in text)
    # В метриках только число: сами идентификаторы — данные харнесса автора.
    metrics = {"identifiers": len(identifiers), "leaked": len(leaked)}
    return CheckOutcome("fail" if leaked else "pass", metrics, "\n".join(leaked))


def provenance(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    prov = _candidate(artifact).get("provenance") or {}
    jobs = prov.get("source_jobs") or []
    summarized = {s.get("job_id") for s in artifact.get("source_job_summaries") or [] if isinstance(s, Mapping)}
    problems = []
    if not jobs:
        problems.append("no source_jobs")
    missing = sorted(j for j in jobs if j not in summarized)
    if missing:
        problems.append(f"jobs without summary: {missing}")
    if not prov.get("created_at"):
        problems.append("no created_at")
    return CheckOutcome("fail" if problems else "pass", {"source_jobs": len(jobs), "unsummarized": len(missing)}, "\n".join(problems))
