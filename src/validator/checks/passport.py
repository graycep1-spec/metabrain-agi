"""Профиль Passport: schema, rights, autonomy (C-4, паспорт — C-6)."""

from collections.abc import Mapping
import re
from typing import Any

from src.validator.checks.shape import NonEmpty, OneOf, Optional, errors
from src.validator.scorecard import Context
from src.validator.service import CheckOutcome


LEVELS = ("L0", "L1", "L2")
CRON = re.compile(r"^\S+(\s+\S+){4}$")

SCOPES = {"connectors": [str], "read_scopes": [str], "write_classes": [str]}

ROLE_PASSPORT = {
    "employee_id": NonEmpty(str),
    "version": int,
    "kind": OneOf("twin", "new", "researcher"),
    "harness_id": NonEmpty(str),
    "owner_person_id": NonEmpty(str),
    "curator_person_id": NonEmpty(str),
    "role": {"title": NonEmpty(str), "mission": NonEmpty(str)},
    "duties": NonEmpty([{"duty_id": NonEmpty(str), "text": NonEmpty(str)}]),
    "goals": [{
        "goal_id": NonEmpty(str),
        "text": NonEmpty(str),
        "metric": NonEmpty(str),
        "target": "any",
        "period": NonEmpty(str),
    }],
    "decision_bounds": {"may": [str], "must_not": [str]},
    "escalation": [{"condition": NonEmpty(str), "to": OneOf("curator")}],
    "tools": SCOPES,
    "triggers": {
        "schedule": [{"cron": NonEmpty(str), "duty_id": NonEmpty(str)}],
        "events": [{"subject": NonEmpty(str), "filter": Optional("any"), "duty_id": NonEmpty(str)}],
    },
    "autonomy": "any",
    "provenance": {
        "source": OneOf("role_text", "template"),
        "drafted_by": NonEmpty(str),
        "consent_ref": Optional(str),
        "created_at": NonEmpty(str),
    },
}

PASSPORT_ARTIFACT = {
    "passport": ROLE_PASSPORT,
    "curator_scopes": SCOPES,
    "previous_passport": Optional(ROLE_PASSPORT),
}


def schema(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    found = errors(artifact, PASSPORT_ARTIFACT)
    passport = artifact.get("passport")
    if not found and isinstance(passport, Mapping):
        duty_ids = {d["duty_id"] for d in passport["duties"]}
        triggers = passport["triggers"]
        for index, item in enumerate(triggers["schedule"]):
            if not CRON.match(item["cron"]):
                found.append(f"$.passport.triggers.schedule[{index}].cron: must have 5 fields")
        for kind in ("schedule", "events"):
            for index, item in enumerate(triggers[kind]):
                if item["duty_id"] not in duty_ids:
                    found.append(f"$.passport.triggers.{kind}[{index}].duty_id: unknown duty")
        if not isinstance(passport["autonomy"], Mapping):
            found.append("$.passport.autonomy: must be an object")
    return CheckOutcome("fail" if found else "pass", {"errors": len(found)}, "\n".join(found))


def rights(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    """Права ЦС ⊆ права куратора; паспорт — того же харнесса (C-010)."""
    passport = artifact.get("passport") or {}
    tools = passport.get("tools") or {}
    curator = artifact.get("curator_scopes") or {}
    excess = {
        name: sorted(set(tools.get(name) or []) - set(curator.get(name) or []))
        for name in ("connectors", "read_scopes", "write_classes")
    }
    excess = {name: items for name, items in excess.items() if items}
    problems = [f"{name} beyond curator: {items}" for name, items in excess.items()]
    if passport.get("harness_id") != str(context.harness_id):
        problems.append("passport belongs to another harness")
    if passport.get("kind") == "researcher" and tools.get("write_classes"):
        problems.append("researcher must not have write_classes")
    return CheckOutcome("fail" if problems else "pass", {"excess": excess}, "\n".join(problems))


def autonomy(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    """Новый паспорт — всё на L0; изменение уровня — только через autonomy_change (C-6)."""
    passport = artifact.get("passport") or {}
    levels = passport.get("autonomy") or {}
    if not isinstance(levels, Mapping):
        return CheckOutcome("fail", {"reason": "malformed"})
    previous = artifact.get("previous_passport")
    before = (previous or {}).get("autonomy") or {}

    invalid = sorted(cls for cls, level in levels.items() if level not in LEVELS)
    if previous is None:
        raised = sorted(cls for cls, level in levels.items() if level != "L0")
        changed: list[str] = []
    else:
        raised = []
        changed = sorted(
            cls for cls in set(levels) | set(before) if levels.get(cls, "L0") != before.get(cls, "L0")
        )
    problems = invalid + raised + changed
    metrics = {"invalid": invalid, "above_l0_on_new": raised, "changed_outside_autonomy_change": changed}
    return CheckOutcome("fail" if problems else "pass", metrics)
