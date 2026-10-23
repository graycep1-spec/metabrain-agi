"""Черновик паспорта роли (C-6, «Черновик и активация паспорта»)."""

from collections.abc import Mapping, Sequence
import json
import re
from typing import Any, Protocol

from src.llm.connector import ChatRequest, LLMConnector, LLMContext
from src.workforce.cron import validate as cron_error


DEFAULT_CRON = "*/15 * * * *"
MAX_TEXT = 500

DRAFT_SYSTEM_PROMPT = """Ты — разработчик в команде цифровых сотрудников. По описанию роли человека составь
черновик паспорта роли. Ответь ТОЛЬКО JSON-объектом без пояснений, строго такой формы:
{
  "role": {"title": "...", "mission": "..."},
  "duties": [{"text": "..."}],
  "goals": [{"text": "...", "metric": "...", "target": "...", "period": "..."}],
  "decision_bounds": {"may": ["..."], "must_not": ["..."]},
  "escalation": [{"condition": "..."}],
  "schedule": [{"cron": "*/15 * * * *", "duty": 1}]
}
Правила: бери только то, что есть в описании; ничего не придумывай. Если чего-то не хватает
(метрика цели, расписание, граница решений) — не заполняй, а добавь пункт в escalation с вопросом
куратору. cron — 5 полей по UTC, duty — номер обязанности с 1."""


class DraftError(ValueError):
    """Ответ модели не сводится к паспорту — паспорт не создаётся."""


class PassportDrafter(Protocol):
    def draft(self, role_text: str, context: LLMContext) -> dict[str, Any]: ...


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DraftError(f"{name} must be a non-empty string")
    return value.strip()[:MAX_TEXT]


def _list(value: Any, name: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DraftError(f"{name} must be a list")
    return value


def parse_draft(content: str, *, strict: bool = False) -> dict[str, Any]:
    """JSON → нормализованный черновик.

    Ответ модели (strict=False): пробелы становятся вопросами куратору в escalation.
    Правка человека (strict=True): пробел или неверный cron — ошибка с указанием поля.
    """
    match = re.search(r"\{.*\}", content or "", re.DOTALL)
    if not match:
        raise DraftError("model answer has no JSON object")
    try:
        raw = json.loads(match.group(0))
    except ValueError as error:
        raise DraftError("model answer is not valid JSON") from error
    if not isinstance(raw, Mapping):
        raise DraftError("model answer must be a JSON object")

    role = raw.get("role")
    if not isinstance(role, Mapping):
        raise DraftError("role is required")
    title, mission = _text(role.get("title"), "role.title"), _text(role.get("mission"), "role.mission")

    duties = []
    for index, duty in enumerate(_list(raw.get("duties"), "duties"), start=1):
        text = duty.get("text") if isinstance(duty, Mapping) else duty
        duties.append({"duty_id": f"d{index}", "text": _text(text, f"duties[{index}]")})
    if not duties:
        raise DraftError("at least one duty is required")

    escalation: list[dict[str, str]] = []
    for item in _list(raw.get("escalation"), "escalation"):
        condition = item.get("condition") if isinstance(item, Mapping) else item
        escalation.append({"condition": _text(condition, "escalation.condition"), "to": "curator"})

    goals = []
    for index, goal in enumerate(_list(raw.get("goals"), "goals"), start=1):
        if not isinstance(goal, Mapping):
            raise DraftError(f"goals[{index}] must be an object")
        text = _text(goal.get("text"), f"goals[{index}].text")
        metric = goal.get("metric")
        period = goal.get("period")
        if strict and (not isinstance(metric, str) or not metric.strip() or not isinstance(period, str) or not period.strip()):
            raise DraftError(f"goals[{index}] needs metric and period")
        if not isinstance(metric, str) or not metric.strip():
            metric = "уточнить у куратора"
            escalation.append({"condition": f"Уточнить метрику цели «{text}»", "to": "curator"})
        if not isinstance(period, str) or not period.strip():
            period = "уточнить у куратора"
            escalation.append({"condition": f"Уточнить период цели «{text}»", "to": "curator"})
        goals.append({
            "goal_id": f"g{index}", "text": text, "metric": metric.strip()[:MAX_TEXT],
            "target": goal.get("target"), "period": period.strip()[:MAX_TEXT],
        })

    bounds = raw.get("decision_bounds") if isinstance(raw.get("decision_bounds"), Mapping) else {}
    decision_bounds = {
        name: [_text(item, f"decision_bounds.{name}") for item in _list(bounds.get(name), f"decision_bounds.{name}")]
        for name in ("may", "must_not")
    }

    schedule = []
    for item in _list(raw.get("schedule"), "schedule"):
        if not isinstance(item, Mapping):
            continue
        expression = item.get("cron")
        number = item.get("duty", 1)
        problem = cron_error(expression) if isinstance(expression, str) else "cron is missing"
        if strict and (problem or not isinstance(number, int) or not 1 <= number <= len(duties)):
            raise DraftError(f"schedule {expression!r}: {problem or 'unknown duty number'}")
        if problem or not isinstance(number, int) or not 1 <= number <= len(duties):
            escalation.append({"condition": f"Уточнить расписание «{expression}»: {problem or 'неизвестная обязанность'}", "to": "curator"})
            continue
        schedule.append({"cron": expression, "duty_id": f"d{number}"})
    if not schedule:
        schedule = [{"cron": DEFAULT_CRON, "duty_id": "d1"}]

    return {
        "role": {"title": title, "mission": mission},
        "duties": duties,
        "goals": goals,
        "decision_bounds": decision_bounds,
        "escalation": escalation,
        "schedule": schedule,
    }


def build_passport(
    draft: Mapping[str, Any],
    *,
    employee_id: str,
    version: int,
    kind: str,
    harness_id: str,
    owner_person_id: str,
    connectors: Sequence[str],
    read_scopes: Sequence[str],
    source: str,
    drafted_by: str,
    created_at: str,
    autonomy: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """RolePassport (C-6): что не решает модель, заполняет харнесс."""
    return {
        "employee_id": employee_id,
        "version": version,
        "kind": kind,
        "harness_id": harness_id,
        "owner_person_id": owner_person_id,
        "curator_person_id": owner_person_id,
        "role": dict(draft["role"]),
        "duties": [dict(d) for d in draft["duties"]],
        "goals": [dict(g) for g in draft["goals"]],
        "decision_bounds": {k: list(v) for k, v in draft["decision_bounds"].items()},
        "escalation": [dict(e) for e in draft["escalation"]],
        "tools": {"connectors": sorted(connectors), "read_scopes": sorted(read_scopes), "write_classes": []},
        "triggers": {"schedule": [dict(s) for s in draft["schedule"]], "events": []},
        "autonomy": dict(autonomy or {}),
        "provenance": {"source": source, "drafted_by": drafted_by, "consent_ref": None, "created_at": created_at},
    }


EDITABLE = ("role", "duties", "goals", "decision_bounds", "escalation", "schedule")


def apply_patch(passport: Mapping[str, Any], patch: Any) -> dict[str, Any]:
    """Правка человека: заменённые секции + остальное из действующей версии; строгая проверка формы."""
    if not isinstance(patch, Mapping) or not patch:
        raise DraftError("passport_patch must be a non-empty object")
    unknown = sorted(set(patch) - set(EDITABLE))
    if unknown:
        raise DraftError(f"these sections are not edited here: {unknown}")
    base = draft_fields(passport)
    merged = {
        "role": base["role"],
        "duties": [{"text": d["text"]} for d in base["duties"]],
        "goals": [{k: g[k] for k in ("text", "metric", "target", "period")} for g in base["goals"]],
        "decision_bounds": base["decision_bounds"],
        "escalation": [{"condition": e["condition"]} for e in base["escalation"]],
        "schedule": [{"cron": s["cron"], "duty": int(s["duty_id"][1:])} for s in base["schedule"]],
    }
    merged.update(patch)
    return parse_draft(json.dumps(merged, ensure_ascii=False), strict=True)


def passport_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Что меняется по секциям: строки списков — добавлено/удалено, значения — было/стало."""
    def lines(passport: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "role.title": passport["role"]["title"],
            "role.mission": passport["role"]["mission"],
            "duties": [d["text"] for d in passport["duties"]],
            "goals": [f"{g['text']} — {g['metric']}: {g.get('target')} за {g['period']}" for g in passport["goals"]],
            "decision_bounds.may": list(passport["decision_bounds"]["may"]),
            "decision_bounds.must_not": list(passport["decision_bounds"]["must_not"]),
            "escalation": [e["condition"] for e in passport["escalation"]],
            "schedule": [f"{s['cron']} → {s['duty_id']}" for s in passport["triggers"]["schedule"]],
            "tools.connectors": list(passport["tools"]["connectors"]),
            "tools.read_scopes": list(passport["tools"]["read_scopes"]),
            "autonomy": [f"{k}: {v}" for k, v in sorted(passport["autonomy"].items())],
        }

    old, new = lines(before), lines(after)
    diff = []
    for field, value in new.items():
        if old[field] == value:
            continue
        if isinstance(value, list):
            diff.append({"field": field, "added": [x for x in value if x not in old[field]],
                         "removed": [x for x in old[field] if x not in value]})
        else:
            diff.append({"field": field, "before": old[field], "after": value})
    return diff


def draft_fields(passport: Mapping[str, Any]) -> dict[str, Any]:
    """Поля паспорта, которые решает модель, — для новой версии на основе действующей."""
    return {
        "role": passport["role"],
        "duties": passport["duties"],
        "goals": passport["goals"],
        "decision_bounds": passport["decision_bounds"],
        "escalation": passport["escalation"],
        "schedule": passport["triggers"]["schedule"],
    }


class LLMPassportDrafter:
    """Разработчик роя: текст роли → черновик через LLM-коннектор (DLP, бюджет — внутри коннектора)."""

    def __init__(self, connector: LLMConnector):
        self._connector = connector

    def draft(self, role_text: str, context: LLMContext) -> dict[str, Any]:
        result = self._connector.chat_completions(context, ChatRequest(messages=[
            {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
            {"role": "user", "content": role_text},
        ]))
        try:
            content = result.response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise DraftError("model answer has no message content") from error
        return parse_draft(content)
