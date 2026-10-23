"""Исследователь: подключение системы, драфт адаптера, применение и discovery (C-3, T-19)."""

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from psycopg import Connection
from psycopg.types.json import Jsonb

from src.common.adapter_process import run_adapter
from src.common.secrets import EnvSecretStore, SecretNotFoundError
from src.harness.journal import JournalStore
from src.harness.proposals import ConsentToken, ProposalStore
from src.harness.store import HarnessStore, Registration
from src.landscape.bridge import make_bridge
from src.landscape.drafter import DraftRejected, draft_adapter, files_sha256
from src.landscape.registry import SystemEntry
from src.validator.client import ValidatorClient, ValidatorUnavailable
from src.validator.scorecard import ScoreCardRejected, verify_scorecard
from src.workforce.store import WorkforceStore


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class LandscapeUnavailable(RuntimeError):
    code = "landscape_unavailable"


def migrate(connection: Connection) -> None:
    with connection.transaction():
        connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"), prepare=False)


class ResearcherService:
    def __init__(
        self,
        connection: Connection,
        *,
        harness: HarnessStore,
        proposals: ProposalStore,
        journal: JournalStore,
        validator: ValidatorClient,
        registry: Mapping[str, SystemEntry],
        secret_store: EnvSecretStore,
        http_client: httpx.Client,
        on_new_system: Callable[[Registration, str], list[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._c = connection
        self._harness = harness
        self._proposals = proposals
        self._journal = journal
        self._validator = validator
        self._registry = dict(registry)
        self._secrets = secret_store
        self._http = http_client
        self._on_new_system = on_new_system
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._workforce = WorkforceStore(connection)
        migrate(connection)

    # --- подключённые системы — для паспортов (C-6) --------------------------------------

    def tools(self, harness_id: UUID) -> tuple[list[str], list[str]]:
        """Системы с активным адаптером и их read_scopes."""
        rows = self._c.execute(
            "SELECT system, artifact FROM lc_adapters WHERE harness_id = %s AND status = 'active' ORDER BY system",
            (harness_id,),
        ).fetchall()
        connectors = [system for system, _ in rows]
        scopes = sorted({scope for _, artifact in rows for scope in artifact["manifest"]["read_scopes"]})
        return connectors, scopes

    def systems(self) -> list[str]:
        """Системы из реестра LANDSCAPE_SYSTEMS — только их можно подключить."""
        return sorted(self._registry)

    def statuses(self, harness_id: UUID) -> list[dict[str, Any]]:
        """Состояние каждой системы реестра для харнесса (C-1 `landscape.map`)."""
        connected = dict(self._c.execute(
            "SELECT system, connected_at FROM lc_connections WHERE harness_id = %s", (harness_id,)
        ).fetchall())
        active = {row[0] for row in self._c.execute(
            "SELECT system FROM lc_adapters WHERE harness_id = %s AND status = 'active'", (harness_id,)
        ).fetchall()}
        pending = {row[0] for row in self._c.execute(
            """
            SELECT subject->>'system' FROM proposals
            WHERE harness_id = %s AND status = 'pending' AND kind IN ('connect', 'adapter')
            """,
            (harness_id,),
        ).fetchall()}
        result = []
        for system in self.systems():
            status = ("active" if system in active else "pending" if system in pending
                      else "connected" if system in connected else "available")
            item: dict[str, Any] = {"system": system, "status": status}
            if system in connected:
                item["connected_at"] = connected[system].isoformat()
            result.append(item)
        return result

    # --- согласие на подключение → драфт адаптера ---------------------------------------------

    def on_connect_approved(self, identity: Registration, *, proposal_id: UUID, token: ConsentToken) -> dict[str, Any]:
        subject = self._proposals.apply_proposal(
            person_id=identity.person_id, harness_id=identity.harness_id, proposal_id=proposal_id, consent_token=token,
        )
        system, secret_ref = subject["system"], subject["secret_ref"]
        entry = self._registry.get(system)
        if entry is None:
            raise ValueError(f"system {system!r} is not in LANDSCAPE_SYSTEMS")
        self._c.execute(
            """
            INSERT INTO lc_connections (harness_id, company_id, system, secret_ref, proposal_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (harness_id, system) DO UPDATE
            SET secret_ref = EXCLUDED.secret_ref, proposal_id = EXCLUDED.proposal_id, connected_at = clock_timestamp()
            """,
            (identity.harness_id, identity.company_id, system, secret_ref, proposal_id),
        )
        self._journal_entry(identity, "landscape.connected", {"system": system, "proposal_id": str(proposal_id)})
        return {"system": system, "connected": True, **self.draft(identity, system=system)}

    def draft(self, identity: Registration, *, system: str) -> dict[str, Any]:
        entry = self._registry[system]
        secret_ref = self._secret_ref(identity.harness_id, system)
        researcher = self._workforce.ensure_researcher(company_id=identity.company_id, harness_id=identity.harness_id)
        schema = self._fetch_schema(entry, secret_ref)
        try:
            artifact = draft_adapter(
                system=system, schema=schema, secret_ref=secret_ref, author=str(researcher.employee_id),
                job_id=f"connect:{system}", created_at=self._clock().isoformat(), test_data_ref=entry.test_data_ref,
            )
        except DraftRejected as error:
            self._journal_entry(identity, "adapter.rejected", {"system": system, "reasons": [str(error)]},
                                employee_id=researcher.employee_id)
            return {"adapter": "rejected", "reasons": [str(error)]}

        context = {"company_id": str(identity.company_id), "harness_id": str(identity.harness_id),
                   "employee_id": str(researcher.employee_id)}
        try:
            scorecard = self._validator.validate(artifact, "adapter", context)
        except ValidatorUnavailable as error:
            raise LandscapeUnavailable(str(error)) from error
        adapter_id = uuid4()
        digest = files_sha256(artifact["files"])
        passed = scorecard.get("verdict") == "pass"
        self._c.execute(
            """
            INSERT INTO lc_adapters (adapter_id, harness_id, company_id, system, artifact, files_sha256, scorecard, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (adapter_id, identity.harness_id, identity.company_id, system, Jsonb(artifact), digest,
             Jsonb(scorecard), "proposed" if passed else "rejected"),
        )
        if not passed:
            reasons = [f"{c['name']}: {c.get('metrics')}" for c in scorecard.get("checks", []) if c.get("verdict") != "pass"]
            self._journal_entry(identity, "adapter.rejected", {"system": system, "reasons": reasons},
                                employee_id=researcher.employee_id, score_ref=scorecard.get("scorecard_id"))
            return {"adapter": "rejected", "reasons": reasons}

        proposal = self._proposals.create_proposal(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="adapter", summary=f"Включить адаптер системы {system} (только чтение)",
            subject={"system": system, "manifest": artifact["manifest"], "files_sha256": digest,
                     "scorecard_id": scorecard["scorecard_id"]},
            score_ref=scorecard["scorecard_id"],
        )
        self._c.execute("UPDATE lc_adapters SET proposal_id = %s WHERE adapter_id = %s", (proposal.id, adapter_id))
        self._journal_entry(identity, "adapter.proposed", {"system": system, "proposal_id": str(proposal.id)},
                            employee_id=researcher.employee_id, score_ref=scorecard["scorecard_id"])
        return {"adapter": "proposed", "adapter_proposal_id": str(proposal.id)}

    # --- согласие на адаптер → активация и discovery -----------------------------------------------

    def on_adapter_approved(self, identity: Registration, *, proposal_id: UUID, token: ConsentToken) -> dict[str, Any]:
        subject = self._proposals.apply_proposal(
            person_id=identity.person_id, harness_id=identity.harness_id, proposal_id=proposal_id, consent_token=token,
        )
        row = self._c.execute(
            """
            SELECT adapter_id, system, artifact, files_sha256, scorecard, status
            FROM lc_adapters WHERE proposal_id = %s AND harness_id = %s
            """,
            (proposal_id, identity.harness_id),
        ).fetchone()
        if row is None:
            raise ValueError("adapter proposal has no stored draft")
        adapter_id, system, artifact, digest, scorecard, status = row
        if status != "proposed" or digest != subject["files_sha256"] or files_sha256(artifact["files"]) != digest:
            raise ValueError("adapter draft changed after the proposal")
        self._check_scorecard(scorecard, artifact, identity.harness_id)

        self._c.execute(
            "UPDATE lc_adapters SET status = 'superseded' WHERE harness_id = %s AND system = %s AND status = 'active'",
            (identity.harness_id, system),
        )
        self._c.execute(
            "UPDATE lc_adapters SET status = 'active', activated_at = clock_timestamp() WHERE adapter_id = %s",
            (adapter_id,),
        )
        researcher = self._workforce.ensure_researcher(company_id=identity.company_id, harness_id=identity.harness_id)
        self._journal_entry(identity, "adapter.activated", {"system": system, "adapter_id": str(adapter_id)},
                            employee_id=researcher.employee_id, score_ref=scorecard["scorecard_id"])

        discovery = self.discover(identity, system=system)
        extension = self._on_new_system(identity, system) if self._on_new_system else []
        return {"system": system, "adapter_id": str(adapter_id), "discovery": discovery,
                "passport_proposals": extension}

    def discover(self, identity: Registration, *, system: str) -> dict[str, Any]:
        """discover() активного адаптера на живой системе — только чтение (R-03)."""
        row = self._c.execute(
            "SELECT adapter_id, artifact FROM lc_adapters WHERE harness_id = %s AND system = %s AND status = 'active'",
            (identity.harness_id, system),
        ).fetchone()
        if row is None:
            raise ValueError(f"no active adapter for {system}")
        adapter_id, artifact = row
        entry = self._registry[system]
        token = self._token(artifact["manifest"]["auth"]["secret_ref"])
        researcher = self._workforce.ensure_researcher(company_id=identity.company_id, harness_id=identity.harness_id)
        calls: list[tuple[str, int]] = []
        bridge = make_bridge(base_url=entry.base_url, token=token, client=self._http,
                             on_call=lambda path, status: calls.append((path, status)))
        run = run_adapter(artifact["files"], operations=["discover"], bridge=bridge)
        for path, status in calls:
            self._journal_entry(identity, "adapter.call", {"system": system, "method": "GET", "path": path,
                                                          "status": status, "operation": "discover"},
                                employee_id=researcher.employee_id)
        delta = run.result.get("discover", {}) if run.ok else {}
        if not run.ok or not delta.get("ok"):
            reason = run.reason or delta.get("error", "discover failed")
            self._journal_entry(identity, "landscape.discovery_failed", {"system": system, "reason": reason},
                                employee_id=researcher.employee_id)
            return {"ok": False, "reason": reason}
        landscape = delta["value"]
        self._harness.append_harness_record(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="landscape",
            payload={"system": system, "adapter_id": str(adapter_id), "objects": landscape.get("objects", []),
                     "discovered_at": self._clock().isoformat()},
        )
        self._journal_entry(identity, "landscape.discovered",
                            {"system": system, "objects": [o.get("name") for o in landscape.get("objects", [])]},
                            employee_id=researcher.employee_id)
        return {"ok": True, "objects": landscape.get("objects", [])}

    # --- факты для роя ЦС (C-6, «Рой ЦС») ------------------------------------------------------------

    def collect_facts(self, *, company_id: UUID, harness_id: UUID, employee_id: UUID,
                      systems: list[str], limit: int = 40) -> list[dict[str, Any]]:
        """discover() → ключи, probe(ref) → факты; только системы с активным адаптером, только чтение."""
        owner = self._c.execute(
            "SELECT owner_person_id FROM harnesses WHERE id = %s AND company_id = %s", (harness_id, company_id)
        ).fetchone()
        if owner is None:
            return []
        identity = Registration(person_id=owner[0], company_id=company_id, harness_id=harness_id)
        facts: list[dict[str, Any]] = []
        for system in systems:
            if len(facts) >= limit or system not in self._registry:
                continue
            row = self._c.execute(
                "SELECT artifact FROM lc_adapters WHERE harness_id = %s AND system = %s AND status = 'active'",
                (harness_id, system),
            ).fetchone()
            if row is None:
                continue
            artifact = row[0]
            calls: list[tuple[str, int]] = []
            bridge = make_bridge(base_url=self._registry[system].base_url,
                                 token=self._token(artifact["manifest"]["auth"]["secret_ref"]),
                                 client=self._http, on_call=lambda path, status: calls.append((path, status)))
            listing = run_adapter(artifact["files"], operations=["discover"], bridge=bridge)
            delta = listing.result.get("discover", {}) if listing.ok else {}
            objects = delta.get("value", {}).get("objects", []) if delta.get("ok") else []
            refs = [f"{o['name']}/{key}" for o in objects for key in o.get("keys", [])][: limit - len(facts)]
            if refs:
                probed = run_adapter(artifact["files"], operations=["probes"], refs=refs, bridge=bridge)
                for ref, outcome in (probed.result.get("probes", {}) if probed.ok else {}).items():
                    value = outcome.get("value") if outcome.get("ok") else None
                    if isinstance(value, Mapping) and isinstance(value.get("data"), Mapping):
                        facts.append({"system": system, **value})
            for path, status in calls:
                self._journal_entry(identity, "adapter.call", {"system": system, "method": "GET", "path": path,
                                                              "status": status, "operation": "collect"},
                                    employee_id=employee_id)
        return facts

    # --- внутреннее -----------------------------------------------------------------------------

    def _secret_ref(self, harness_id: UUID, system: str) -> str:
        row = self._c.execute(
            "SELECT secret_ref FROM lc_connections WHERE harness_id = %s AND system = %s", (harness_id, system)
        ).fetchone()
        if row is None:
            raise ValueError(f"system {system!r} is not connected")
        return row[0]

    def _token(self, secret_ref: str) -> str:
        try:
            return self._secrets.resolve(secret_ref)
        except SecretNotFoundError as error:
            raise LandscapeUnavailable("connection secret is not configured") from error

    def _fetch_schema(self, entry: SystemEntry, secret_ref: str) -> Any:
        token = self._token(secret_ref)
        try:
            response = self._http.get(f"{entry.base_url}/schema", headers={"Authorization": f"Bearer {token}"},
                                      follow_redirects=False)
        except httpx.HTTPError as error:
            raise LandscapeUnavailable(f"system {entry.system} is unreachable") from error
        if response.status_code != 200:
            raise LandscapeUnavailable(f"system {entry.system} answered {response.status_code} to /schema")
        return response.json()

    def _check_scorecard(self, scorecard: Mapping[str, Any], artifact: Mapping[str, Any], harness_id: UUID) -> None:
        for refresh in (False, True):
            try:
                keys = self._validator.keys(refresh=refresh)
            except ValidatorUnavailable as error:
                raise LandscapeUnavailable(str(error)) from error
            try:
                verify_scorecard(scorecard, keys, artifact=artifact, harness_id=harness_id)
                return
            except ScoreCardRejected as error:
                if error.code == "unknown_key" and not refresh:
                    continue
                raise ValueError(f"adapter scorecard rejected: {error.code}") from error

    def _journal_entry(self, identity: Registration, kind: str, payload: Mapping[str, Any], *,
                       employee_id: UUID | None = None, score_ref: str | None = None) -> None:
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind=kind, payload=dict(payload), employee_id=employee_id, score_ref=score_ref,
        )
