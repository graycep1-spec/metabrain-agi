"""Навыки из работы (T-23; C-6 «Навыки из работы», C-5, ADR-027).

Одобренная куратором рекомендация → разработчик роя обобщает её в навык → личная версия публикуется сразу,
версия для компании проходит профиль Skill (foreign_data) и уходит куратору предложением `skill_publish`;
после согласия мета-мозг публикует её с `consent_token`, и навык виден всем харнессам компании.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import re
from typing import Any
from uuid import UUID

import httpx
from psycopg import Connection

from src.harness.journal import JournalStore
from src.harness.proposals import ConsentToken, ProposalStore
from src.harness.store import Registration
from src.llm.connector import BudgetExceededError, ChatRequest, LLMConnector, LLMContext, ProviderResponseError
from src.metabrain.client import MetabrainClient
from src.metabrain.service import Conflict, MetabrainError, Unavailable
from src.workforce.service import WorkforceUnavailable
from src.workforce.swarm import _json, connector_facts, fact_id, fact_lines


SKILL_PROMPT = """Ты — разработчик цифровых сотрудников. Куратор одобрил рекомендацию сотрудника «{title}»:
«{text}»

Факты, на которых она основана:
{facts}

Обобщи рекомендацию в навык — правило, которое пригодится в похожих случаях другим сотрудникам компании.
Ответь ТОЛЬКО JSON: {{"title": "...", "body": "...", "task_class": "...", "role_tags": ["..."], "examples": ["..."]}}.
Правила: body — шаги и проверки «как делать». Без названий клиентов, имён, телефонов, почты, номеров заявок
и других идентификаторов из фактов — только общее правило. task_class — одно слово латиницей (например quote),
role_tags — 1–3 слова латиницей. Значения «[REDACTED]» — скрытые данные, не упоминай их. Пиши по-русски."""

TASK_CLASS = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class SkillNotFound(LookupError):
    """Навыка нет, он не личный или не свой — 404."""


def clean_skill(raw: Any) -> dict[str, Any]:
    """Ответ разработчика → поля навыка; пустой или чужой формат — ошибка."""
    if not isinstance(raw, Mapping):
        raise ValueError("skill draft must be an object")
    title = raw.get("title").strip()[:120] if isinstance(raw.get("title"), str) else ""
    body = raw.get("body").strip()[:2000] if isinstance(raw.get("body"), str) else ""
    if not title or not body:
        raise ValueError("skill draft has no title or body")
    task_class = raw.get("task_class") if isinstance(raw.get("task_class"), str) else ""
    def strings(value: Any, size: int) -> list[str]:
        return [v.strip()[:size] for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []

    tags, examples = strings(raw.get("role_tags"), 40), strings(raw.get("examples"), 400)
    return {"title": title, "body": body,
            "task_class": task_class.strip().lower() if TASK_CLASS.match(task_class.strip().lower()) else "general",
            "role_tags": tags[:3], "examples": examples[:3]}


def identifiers(facts: Sequence[Mapping[str, Any]]) -> list[str]:
    """Для foreign_data (ADR-027): значения чувствительных полей и ключи объектов с такими полями."""
    def leaves(value: Any) -> list[str]:
        if isinstance(value, Mapping):
            return [leaf for item in value.values() for leaf in leaves(item)]
        if isinstance(value, list):
            return [leaf for item in value for leaf in leaves(item)]
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return [str(value).strip()]
        return []

    found: set[str] = set()
    for fact in facts:
        sensitivity = fact.get("sensitivity") or {}
        if not sensitivity:
            continue
        data = fact.get("data") or {}
        for name in sensitivity:
            found.update(leaves(data.get(name)))
        found.update({fact["ref"], fact["ref"].rsplit("/", 1)[-1]})
    return sorted(found)


def failed_checks(scorecard: Mapping[str, Any]) -> list[str]:
    return [c["name"] for c in scorecard.get("checks", []) if c.get("verdict") != "pass"]


class SkillFlow:
    def __init__(
        self,
        connection: Connection,
        *,
        connector: LLMConnector,
        metabrain: MetabrainClient,
        proposals: ProposalStore,
        journal: JournalStore,
        clock: Callable[[], datetime] | None = None,
    ):
        self._c = connection
        self._connector = connector
        self._metabrain = metabrain
        self._proposals = proposals
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def on_recommendation_approved(self, identity: Registration, *, proposal_id: UUID,
                                   token: ConsentToken) -> dict[str, Any]:
        """Согласие на рекомендацию применяется; из неё рождается навык. Сбой навыка не отменяет решение куратора."""
        subject = self._proposals.apply_proposal(
            person_id=identity.person_id, harness_id=identity.harness_id, proposal_id=proposal_id, consent_token=token,
        )
        employee_id, job_id = UUID(subject["employee_id"]), str(subject["job_id"])
        facts = self._job_facts(identity, job_id)
        try:
            draft = self._draft(identity, subject, facts, employee_id=employee_id, job_id=job_id)
            personal = self._personal(identity, draft, employee_id=employee_id, job_id=job_id)
            if personal["status"] != "published":
                return {"skill": {"personal": personal}}
            company = self._company(identity, draft, {job_id: facts}, employee_id=employee_id)
        except (BudgetExceededError, ProviderResponseError, MetabrainError, httpx.HTTPError, ValueError) as error:
            reason = getattr(error, "code", None) or type(error).__name__
            self._log(identity, "skill.failed", {"proposal_id": str(proposal_id), "job_id": job_id, "reason": reason},
                      employee_id=employee_id)
            return {"skill": {"status": "failed", "reason": reason}}
        return {"skill": {"personal": personal, "company": company}}

    def on_skill_publish_approved(self, identity: Registration, *, proposal_id: UUID,
                                  token: ConsentToken) -> dict[str, Any]:
        """Согласие куратора передаётся мета-мозгу (C-5); мета-мозг недоступен — решение не фиксируется (503)."""
        subject = self._proposals.apply_proposal(
            person_id=identity.person_id, harness_id=identity.harness_id, proposal_id=proposal_id, consent_token=token,
        )
        request = {"company_id": str(identity.company_id), "harness_id": str(identity.harness_id),
                   "candidate_id": subject["candidate_id"], "consent_token": self._proposals.token_document(token)}
        try:
            published = self._metabrain.publish(request)
        except Unavailable as error:
            raise WorkforceUnavailable(f"metabrain: {error.code}") from error
        except Conflict as error:
            if error.code != "already_published":
                raise WorkforceUnavailable(f"metabrain: {error.code}") from error
            published = {"skill_id": None, "version": None}
        except MetabrainError as error:
            self._log(identity, "skill.failed", {"proposal_id": str(proposal_id), "scope": "company", "reason": error.code})
            return {"skill": {"status": "failed", "reason": error.code}}
        self._log(identity, "skill.published", {"scope": "company", "proposal_id": str(proposal_id),
                                                "candidate_id": subject["candidate_id"], **published})
        return {"skill": {"status": "published", "scope": "company", **published}}

    # --- кабинет: экран «Навыки» (T-34) ------------------------------------------------------

    def list_skills(self, identity: Registration, *, scope: str | None = None) -> list[dict[str, Any]]:
        """Проекция C-5 `skills.resolve` для кабинета: последние версии, видимые харнессу."""
        if scope not in (None, "personal", "company"):
            raise ValueError("scope must be personal or company")
        try:
            found = self._metabrain.resolve({"company_id": str(identity.company_id),
                                             "harness_id": str(identity.harness_id), "limit": 50})
        except Unavailable as error:
            raise WorkforceUnavailable(f"metabrain: {error.code}") from error
        result = []
        for skill in found.get("skills", []):
            if scope and skill["scope"] != scope:
                continue
            provenance = skill.get("provenance") or {}
            mine = (skill.get("owner_harness_id") == str(identity.harness_id)
                    or skill.get("author_person_id") == str(identity.person_id))
            result.append({
                "skill_id": skill["skill_id"], "version": skill["version"], "scope": skill["scope"],
                "title": skill["title"], "body": skill["body"], "applies_to": skill["applies_to"],
                "examples": skill.get("examples") or [], "created_at": skill["created_at"],
                "source_jobs": list(provenance.get("source_jobs") or []), "consent": bool(provenance.get("consent_ref")),
                "mine": mine,
            })
        return result

    def share(self, identity: Registration, *, skill_id: str) -> dict[str, Any]:
        """Личный навык → кандидат для компании по фактам его исходных работ (C-6 «Навыки из работы», шаг 3)."""
        skill = next((s for s in self.list_skills(identity, scope="personal") if s["skill_id"] == skill_id), None)
        if skill is None or not skill["mine"]:
            raise SkillNotFound(skill_id)
        draft = {"title": skill["title"], "body": skill["body"], "task_class": skill["applies_to"]["task_class"],
                 "role_tags": list(skill["applies_to"]["role_tags"]), "examples": [str(e) for e in skill["examples"]]}
        jobs = {job_id: self._job_facts(identity, job_id) for job_id in skill["source_jobs"]}
        try:
            return self._company(identity, draft, jobs, employee_id=None)
        except Unavailable as error:
            raise WorkforceUnavailable(f"metabrain: {error.code}") from error

    # --- внутреннее --------------------------------------------------------------------

    def _job_facts(self, identity: Registration, job_id: str) -> list[dict[str, Any]]:
        try:
            job = UUID(job_id)
        except ValueError:
            return []
        row = self._c.execute(
            """
            SELECT s.result FROM wf_job_steps s JOIN wf_jobs j ON j.job_id = s.job_id
            WHERE s.job_id = %s AND j.harness_id = %s AND s.name = 'collect' AND s.status = 'done'
            """,
            (job, identity.harness_id),
        ).fetchone()
        return list(row[0] or []) if row else []

    def _draft(self, identity: Registration, subject: Mapping[str, Any], facts: Sequence[Mapping[str, Any]], *,
               employee_id: UUID, job_id: str) -> dict[str, Any]:
        used = [f for f in facts if fact_id(f) in set(subject.get("source_ids") or [])]
        title = self._c.execute(
            "SELECT title FROM wf_employees WHERE employee_id = %s AND harness_id = %s", (employee_id, identity.harness_id)
        ).fetchone()
        context = LLMContext(company_id=str(identity.company_id), harness_id=str(identity.harness_id),
                             employee_id=str(employee_id), job_id=job_id, sub_role="developer")
        result = self._connector.chat_completions(context, ChatRequest(
            messages=[{"role": "user", "content": SKILL_PROMPT.format(
                title=title[0] if title else "Сотрудник", text=subject["text"], facts=fact_lines(used),
            )}],
            facts=connector_facts(used),
        ))
        try:
            raw = _json(result.response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("developer answer is malformed") from error
        return clean_skill(raw)

    def _candidate(self, identity: Registration, draft: Mapping[str, Any], *, scope: str,
                   job_ids: Sequence[str]) -> dict[str, Any]:
        data: dict[str, Any] = {
            "scope": scope, "company_id": str(identity.company_id), "title": draft["title"],
            "applies_to": {"role_tags": list(draft["role_tags"]), "task_class": draft["task_class"]},
            "body": draft["body"], "examples": list(draft["examples"]),
            "provenance": {"source_jobs": list(job_ids), "scorecard_ref": None, "consent_ref": None,
                           "created_at": self._clock().isoformat()},
        }
        if scope == "personal":
            data["owner_harness_id"] = str(identity.harness_id)
        else:
            data["author_person_id"] = str(identity.person_id)
        return data

    def _personal(self, identity: Registration, draft: Mapping[str, Any], *, employee_id: UUID,
                  job_id: str) -> dict[str, Any]:
        """Личный навык: только ScoreCard pass, без отдельного согласия (C-5). Идентификаторы не нужны — не отдаём."""
        caller = {"company_id": str(identity.company_id), "harness_id": str(identity.harness_id)}
        proposed = self._metabrain.propose({**caller, "candidate": self._candidate(identity, draft, scope="personal", job_ids=[job_id]),
                                            "source_job_summaries": [{"job_id": job_id, "identifiers": []}]})
        score_ref = proposed["scorecard"]["scorecard_id"]
        if proposed["verdict"] != "pass":
            reasons = failed_checks(proposed["scorecard"])
            self._log(identity, "skill.rejected", {"scope": "personal", "job_id": job_id, "reasons": reasons},
                      employee_id=employee_id, score_ref=score_ref)
            return {"status": "rejected", "reasons": reasons}
        published = self._metabrain.publish({**caller, "candidate_id": proposed["candidate_id"]})
        self._log(identity, "skill.published", {"scope": "personal", "job_id": job_id, "title": draft["title"], **published},
                  employee_id=employee_id, score_ref=score_ref)
        return {"status": "published", **published}

    def _company(self, identity: Registration, draft: Mapping[str, Any], jobs: Mapping[str, Sequence[Mapping[str, Any]]],
                 *, employee_id: UUID | None) -> dict[str, Any]:
        """Навык компании: foreign_data по идентификаторам исходных работ; pass — предложение `skill_publish` куратору."""
        caller = {"company_id": str(identity.company_id), "harness_id": str(identity.harness_id)}
        summaries = [{"job_id": job_id, "identifiers": identifiers(facts)} for job_id, facts in jobs.items()]
        proposed = self._metabrain.propose({**caller, "source_job_summaries": summaries,
                                            "candidate": self._candidate(identity, draft, scope="company", job_ids=list(jobs))})
        score_ref = proposed["scorecard"]["scorecard_id"]
        if proposed["verdict"] != "pass":
            reasons = failed_checks(proposed["scorecard"])
            self._log(identity, "skill.rejected", {"scope": "company", "source_jobs": list(jobs), "reasons": reasons},
                      employee_id=employee_id, score_ref=score_ref)
            return {"status": "rejected", "reasons": reasons}
        proposal = self._proposals.create_proposal(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="skill_publish", summary=f"Поделиться навыком с компанией: «{draft['title']}». {draft['body']}"[:1000],
            subject={"candidate_id": proposed["candidate_id"], "artifact_hash": proposed["artifact_hash"]},
            score_ref=score_ref,
        )
        return {"status": "proposed", "proposal_id": str(proposal.id)}

    def _log(self, identity: Registration, kind: str, payload: Mapping[str, Any], *,
             employee_id: UUID | None = None, score_ref: str | None = None) -> None:
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind=kind, employee_id=employee_id, payload=dict(payload), score_ref=score_ref,
        )
