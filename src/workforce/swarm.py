"""Рой цифрового сотрудника: collect → skills → analyze → review → validate → propose (C-6, «Рой ЦС»; ADR-026).

Аналитик и критик — LLM через коннектор (DLP и бюджет — внутри). Факты попадают в промпт только
плейсхолдерами `{{fact:<система>:<ref>.<поле>}}`. Текст ответа собирается только из утверждений
со ссылками на факты; что не подтверждено — не выдаётся. Навыки (T-23, ADR-027) — ещё один вид источника.
"""

from collections.abc import Callable, Mapping, Sequence
import json
import re
from typing import Any
from uuid import UUID

from psycopg import Connection

from src.harness.journal import JournalStore
from src.harness.proposals import ProposalStore
from src.harness.store import Registration
from src.llm.connector import ChatRequest, LLMConnector, LLMContext
from src.metabrain.service import MetabrainError
from src.validator.client import ValidatorClient
from src.validator.scorecard import ScoreCardRejected, verify_scorecard
from src.workforce.runtime import Steps
from src.workforce.store import Job


MAX_CLAIMS = 12
MAX_TEXT = 400
MAX_SKILLS = 5
MAX_SKILL_TEXT = 1500

ANALYST_PROMPT = """Ты — цифровой сотрудник «{title}». Миссия: {mission}.
Тебе можно: {may}. Нельзя: {must_not}.
Задача: {task}

Факты из систем (у каждого — идентификатор в квадратных скобках):
{facts}{skills}

Ответь ТОЛЬКО JSON-объектом:
{{"claims": [{{"text": "...", "sources": ["<идентификатор факта>", ...]}}],
  "recommendation": {{"text": "...", "sources": ["..."]}} или null}}
Правила: каждое утверждение — одно проверяемое предложение, которое прямо следует из указанных фактов;
без ссылки на факт утверждение не пиши. Ничего не выдумывай. Если данных не хватает — так и скажи
в утверждении со ссылкой на то, что есть. Рекомендация — только в пределах того, что тебе можно.
Значения «[REDACTED]» — скрытые данные, не пытайся их угадать. Пиши по-русски."""

CRITIC_PROMPT = """Проверь утверждения по фактам. Факты:
{facts}

Утверждения (номер: текст [источники]):
{claims}

Ответь ТОЛЬКО JSON: {{"unsupported": [номера утверждений, которые факты не подтверждают], "notes": "..."}}.
Утверждение подтверждено, только если оно прямо следует из указанных у него фактов."""


def _json(content: str) -> Any:
    match = re.search(r"\{.*\}", content or "", re.DOTALL)
    if not match:
        raise ValueError("model answer has no JSON object")
    return json.loads(match.group(0))


def fact_id(fact: Mapping[str, Any]) -> str:
    return f"{fact['system']}:{fact['ref']}"


def fact_lines(facts: Sequence[Mapping[str, Any]]) -> str:
    """Факты плейсхолдерами: коннектор подставит значения после DLP (C-2)."""
    lines = []
    for fact in facts:
        ident = fact_id(fact)
        fields = ", ".join(f"{name}={{{{fact:{ident}.{name}}}}}" for name in fact["data"])
        lines.append(f"[{ident}] {fields}")
    return "\n".join(lines) or "(фактов нет)"


def skill_ref(skill: Mapping[str, Any]) -> str:
    return f"{skill['skill_id']}@{skill['version']}"


def skill_source_id(skill: Mapping[str, Any]) -> str:
    return f"skill:{skill_ref(skill)}"


def skill_lines(skills: Sequence[Mapping[str, Any]]) -> str:
    """Навыки — обычным текстом: они прошли профиль Skill (без ПДн); `{{` гасится, чтобы не стать плейсхолдером."""
    if not skills:
        return ""
    lines = [f"[{skill_source_id(s)}] «{s['title']}»: {s['body']}".replace("{{", "{ {") for s in skills]
    return "\n\nНавыки (правила, проверенные в работе; на них тоже можно ссылаться по идентификатору):\n" + "\n".join(lines)


def visible_skills(metabrain: Any, *, company_id: Any, harness_id: Any, limit: int = MAX_SKILLS) -> list[dict[str, Any]]:
    """Последние видимые харнессу навыки (C-5 `skills.resolve`); мета-мозг недоступен — работаем без навыков."""
    try:
        found = metabrain.resolve({"company_id": str(company_id), "harness_id": str(harness_id), "limit": limit})
    except MetabrainError:
        return []
    return [{"skill_id": s["skill_id"], "version": s["version"], "scope": s["scope"], "title": s["title"][:200],
             "body": s["body"][:MAX_SKILL_TEXT], "created_at": s["created_at"]} for s in found.get("skills", [])[:limit]]


def connector_facts(facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"ref": fact_id(f), "data": f["data"], "sensitivity": f.get("sensitivity", {})} for f in facts]


def clean_claims(raw: Any, known: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any] | None, int]:
    """Только утверждения со ссылками на известные факты; остальное отбрасывается сразу."""
    if not isinstance(raw, Mapping):
        raise ValueError("analysis must be an object")
    dropped = 0
    claims = []
    for item in raw.get("claims") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("text"), str) or not item["text"].strip():
            dropped += 1
            continue
        sources = [s for s in item.get("sources") or [] if isinstance(s, str) and s in known]
        if not sources:
            dropped += 1
            continue
        claims.append({"text": item["text"].strip()[:MAX_TEXT], "sources": sorted(set(sources))})
    recommendation = None
    rec = raw.get("recommendation")
    if isinstance(rec, Mapping) and isinstance(rec.get("text"), str) and rec["text"].strip():
        sources = [s for s in rec.get("sources") or [] if isinstance(s, str) and s in known]
        if sources:
            recommendation = {"text": rec["text"].strip()[:MAX_TEXT], "sources": sorted(set(sources))}
        else:
            dropped += 1
    return claims[:MAX_CLAIMS], recommendation, dropped


def build_answer(claims: Sequence[Mapping[str, Any]], recommendation: Mapping[str, Any] | None,
                 facts: Sequence[Mapping[str, Any]], skills: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Artifact Answer (C-4): текст — только из утверждений, у каждого — источники."""
    by_id = {fact_id(f): f for f in facts}
    by_skill = {skill_source_id(s): s for s in skills}
    items = [dict(c) for c in claims] + ([{**recommendation, "recommendation": True}] if recommendation else [])
    lines, answer_claims, used = [], [], []
    for index, claim in enumerate(items, start=1):
        refs = ", ".join(f"навык «{by_skill[s]['title']}»" if s in by_skill else f"{by_id[s]['system']} {by_id[s]['ref']}"
                         for s in claim["sources"])
        prefix = "Рекомендация: " if claim.get("recommendation") else "• "
        lines.append(f"{prefix}{claim['text']} (источник: {refs})")
        answer_claims.append({"claim_id": f"c{index}", "text": claim["text"], "source_ids": claim["sources"]})
        used.extend(s for s in claim["sources"] if s not in used)
    sources = [{"source_id": s, "system": "skill", "object_ref": skill_ref(by_skill[s]), "skill_ref": skill_ref(by_skill[s]),
                "read_at": by_skill[s]["created_at"]} if s in by_skill else
               {"source_id": s, "system": by_id[s]["system"], "object_ref": by_id[s]["ref"],
                "read_at": (by_id[s].get("source") or {}).get("read_at") or "unknown"} for s in used]
    return {"text": "\n".join(lines), "claims": answer_claims, "sources": sources}


class SwarmHandler:
    """Обработчик обязанности для runtime (C-6) и для `ask`."""

    def __init__(
        self,
        connection: Connection,
        *,
        connector: LLMConnector,
        validator: ValidatorClient,
        collect: Callable[..., list[dict[str, Any]]],
        proposals: ProposalStore,
        journal: JournalStore,
        skills: Callable[..., list[dict[str, Any]]] | None = None,
    ):
        self._c = connection
        self._connector = connector
        self._validator = validator
        self._collect = collect
        self._proposals = proposals
        self._journal = journal
        # Навыки из мета-мозга (T-23): `visible_skills` с клиентом; без мета-мозга — пусто.
        self._skills = skills

    def run(self, job: Job, passport: Mapping[str, Any], steps: Steps) -> dict[str, Any]:
        task = self._task(job, passport)
        facts = steps.run("collect", lambda key: self._collect(
            company_id=job.company_id, harness_id=job.harness_id, employee_id=job.employee_id,
            systems=list(passport["tools"]["connectors"]),
        ))
        if not facts:
            return self._blocked(job, "Нет данных: у сотрудника пока нет подключённых систем или они пусты.", [])
        skills = steps.run("skills", lambda key: self._skills(
            company_id=job.company_id, harness_id=job.harness_id) if self._skills else [])
        known = {fact_id(f) for f in facts} | {skill_source_id(s) for s in skills}

        analysis = steps.run("analyze", lambda key: self._ask_model(job, "analyst", ANALYST_PROMPT.format(
            title=passport["role"]["title"], mission=passport["role"]["mission"],
            may="; ".join(passport["decision_bounds"]["may"]) or "—",
            must_not="; ".join(passport["decision_bounds"]["must_not"]) or "—",
            task=task, facts=fact_lines(facts), skills=skill_lines(skills),
        ), facts))
        claims, recommendation, _ = clean_claims(analysis, known)
        if not claims and not recommendation:
            return self._blocked(job, "Не удалось подтвердить ответ источниками.", [])

        numbered = claims + ([recommendation] if recommendation else [])
        review = steps.run("review", lambda key: self._ask_model(job, "critic", CRITIC_PROMPT.format(
            facts=fact_lines(facts) + skill_lines(skills),
            claims="\n".join(f"{i}: {c['text']} {c['sources']}" for i, c in enumerate(numbered, start=1)),
        ), facts))
        unsupported = {n for n in (review.get("unsupported") or []) if isinstance(n, int)} if isinstance(review, Mapping) else set()
        claims = [c for i, c in enumerate(claims, start=1) if i not in unsupported]
        if recommendation and len(numbered) in unsupported:
            recommendation = None
        if not claims and not recommendation:
            return self._blocked(job, "Критик не подтвердил ни одного утверждения по источникам.", [])

        answer = build_answer(claims, recommendation, facts, skills)
        context = {"company_id": str(job.company_id), "harness_id": str(job.harness_id),
                   "employee_id": str(job.employee_id), "job_id": str(job.job_id)}
        scorecard = steps.run("validate", lambda key: self._validator.validate(answer, "answer", context))
        try:
            self._verify(scorecard, answer, job.harness_id)
        except ScoreCardRejected as error:
            reasons = [f"{c['name']}: {c.get('metrics')}" for c in scorecard.get("checks", []) if c.get("verdict") != "pass"]
            return self._blocked(job, "Ответ не прошёл проверку валидатора.", reasons or [error.code],
                                 score_ref=scorecard.get("scorecard_id"))

        proposal_ids: list[str] = []
        if recommendation:
            rec_sources = [s for s in answer["sources"] if s["source_id"] in recommendation["sources"]]
            proposal_ids = steps.run("propose", lambda key: [str(self._proposals.create_proposal(
                person_id=self._owner(job), company_id=job.company_id, harness_id=job.harness_id,
                kind="recommendation", summary=recommendation["text"][:200],
                subject={"employee_id": str(job.employee_id), "job_id": str(job.job_id),
                         "text": recommendation["text"], "source_ids": recommendation["sources"]},
                sources=rec_sources, score_ref=scorecard["scorecard_id"],
            ).id)])
        return {"text": answer["text"], "sources": answer["sources"], "score_ref": scorecard["scorecard_id"],
                "proposal_ids": proposal_ids, "blocked": False}

    # --- внутреннее -----------------------------------------------------------------------

    @staticmethod
    def _task(job: Job, passport: Mapping[str, Any]) -> str:
        if job.trigger == "ask":
            return f"Ответь на вопрос куратора: {job.input.get('text', '')}"
        duty = next((d["text"] for d in passport["duties"] if d["duty_id"] == job.duty_id), job.duty_id)
        return f"Выполни обязанность «{duty}» и коротко доложи куратору, что нашёл и что рекомендуешь."

    def _ask_model(self, job: Job, sub_role: str, prompt: str, facts: Sequence[Mapping[str, Any]]) -> Any:
        context = LLMContext(company_id=str(job.company_id), harness_id=str(job.harness_id),
                             employee_id=str(job.employee_id), job_id=str(job.job_id), sub_role=sub_role)
        result = self._connector.chat_completions(context, ChatRequest(
            messages=[{"role": "user", "content": prompt}], facts=connector_facts(facts),
        ))
        try:
            return _json(result.response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError):
            return {}

    def _verify(self, scorecard: Mapping[str, Any], answer: Mapping[str, Any], harness_id: UUID) -> None:
        """Подпись ScoreCard до выдачи (C-4); при неизвестном ключе — перечитать ключи один раз."""
        try:
            verify_scorecard(scorecard, self._validator.keys(), artifact=answer, harness_id=harness_id)
        except ScoreCardRejected as error:
            if error.code != "unknown_key":
                raise
            verify_scorecard(scorecard, self._validator.keys(refresh=True), artifact=answer, harness_id=harness_id)

    def _owner(self, job: Job) -> UUID:
        return self._c.execute(
            "SELECT owner_person_id FROM harnesses WHERE id = %s AND company_id = %s", (job.harness_id, job.company_id)
        ).fetchone()[0]

    def _blocked(self, job: Job, message: str, reasons: list[str], score_ref: str | None = None) -> dict[str, Any]:
        """R-04: ответ без подтверждения не выдаётся — человеку сообщение о блоке, в журнал answer.blocked."""
        identity = Registration(person_id=self._owner(job), company_id=job.company_id, harness_id=job.harness_id)
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="answer.blocked", employee_id=job.employee_id,
            payload={"job_id": str(job.job_id), "trigger": job.trigger, "message": message, "reasons": reasons},
            score_ref=score_ref,
        )
        return {"text": message, "sources": [], "score_ref": score_ref, "proposal_ids": [], "blocked": True,
                "reasons": reasons}
