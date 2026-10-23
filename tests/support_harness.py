"""Общая обвязка тестов T-13/T-15/T-19: харнесс на PostgreSQL, валидатор в процессе, демо-ландшафт."""

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi.testclient import TestClient
import psycopg
import pytest

from src.common.secrets import EnvSecretStore
from src.demo_landscape.app import Grant, create_app as create_landscape
from src.demo_landscape.data import Landscape
from src.harness.journal import JournalStore
from src.harness.proposals import ProposalStore
from src.harness.store import HarnessStore, Registration
from src.harness.vault import HarnessVault, SecretStore
from src.landscape.registry import SystemEntry
from src.landscape.service import ResearcherService
from src.workforce.service import WorkforceService
from tests.test_metabrain import InProcessValidator


CONSENT_KEY = base64.b64encode(
    Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
).decode()
CONSENT_PUBLIC_KEY = base64.b64encode(
    Ed25519PrivateKey.from_private_bytes(base64.b64decode(CONSENT_KEY))
    .public_key()
    .public_bytes(Encoding.Raw, PublicFormat.Raw)
).decode()
LANDSCAPE_TOKEN_A = "demo-landscape-token-manager-a"
LANDSCAPE_TOKEN_B = "demo-landscape-token-manager-b"

DRAFT = {
    "role": {"title": "Менеджер по продажам", "mission": "Готовить КП по заявкам"},
    "duties": [{"duty_id": "d1", "text": "Разбирать новые заявки"}],
    "goals": [{"goal_id": "g1", "text": "КП за 2 часа", "metric": "quote_hours", "target": 2, "period": "day"}],
    "decision_bounds": {"may": ["предлагать аналоги"], "must_not": ["скидка ниже min_margin"]},
    "escalation": [{"condition": "нет позиции и аналога", "to": "curator"}],
    "schedule": [{"cron": "*/15 * * * *", "duty_id": "d1"}],
}


class FakeDrafter:
    def __init__(self, draft: dict | None = None):
        self.draft_value = draft or DRAFT
        self.calls = []

    def draft(self, role_text, context):
        self.calls.append((role_text, context))
        return dict(self.draft_value)


class Clock:
    def __init__(self, now: datetime | None = None):
        self.now = now or datetime(2026, 10, 21, 12, 0, 30, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@dataclass
class Env:
    schema: str
    connection: Any
    harness: HarnessStore
    proposals: ProposalStore
    journal: JournalStore
    validator: InProcessValidator
    drafter: FakeDrafter
    workforce: WorkforceService
    researcher: ResearcherService
    landscape_http: TestClient
    clock: Clock
    vault: HarnessVault | None = None
    extra_connections: list = field(default_factory=list)

    def register(self, suffix: str = "a") -> Registration:
        return self.harness.register_person(
            channel="telegram", channel_user_id=f"10{suffix.encode().hex()}", company_name="ООО Пример"
        )

    def approve(self, identity: Registration, proposal_id):
        decision = self.proposals.decide(
            person_id=identity.person_id, harness_id=identity.harness_id, proposal_id=proposal_id,
            decision="approve", actor_person_id=identity.person_id, surface="web",
        )
        return decision.consent_token

    def connect(self, identity: Registration, system: str = "crm", secret_ref: str = "env:CRM_TOKEN_A"):
        return self.proposals.create_proposal(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="connect", summary=f"Подключить {system}", subject={"system": system, "secret_ref": secret_ref},
        )

    def connect_new_connection(self, autocommit: bool = True):
        connection = psycopg.connect(os.environ["TEST_DATABASE_URL"], options=f"-c search_path={self.schema}",
                                     autocommit=autocommit)
        self.extra_connections.append(connection)
        return connection


def registry(base: str = "http://demo-landscape") -> dict[str, SystemEntry]:
    return {
        system: SystemEntry(system, f"{base}/{system}", "demo-landscape:fixture-v1")
        for system in ("crm", "stock", "pricing")
    }


@contextmanager
def harness_env(*, drafter: FakeDrafter | None = None, validator: InProcessValidator | None = None,
                vault_key: bytes | None = None) -> Iterator[Env]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL contract tests")
    schema = f"test_wf_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection = psycopg.connect(url, options=f"-c search_path={schema}")
    env = None
    try:
        clock = Clock()
        secrets = EnvSecretStore({"CONSENT_SIGNING_KEY": CONSENT_KEY, "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY,
                                  "CRM_TOKEN_A": LANDSCAPE_TOKEN_A, "STOCK_TOKEN_A": LANDSCAPE_TOKEN_A,
                                  "CRM_TOKEN_B": LANDSCAPE_TOKEN_B})
        harness = HarnessStore(connection)
        journal = JournalStore(connection)
        # Хранилище секретов харнесса (ADR-028): с ключом — доступы из кабинета, без — только env:.
        vault = HarnessVault(connection, vault_key)
        proposals = ProposalStore(connection, secret_store=secrets, signing_key_ref="env:CONSENT_SIGNING_KEY",
                                  token_ttl=timedelta(days=3650))
        validator = validator or InProcessValidator()
        drafter = drafter or FakeDrafter()
        landscape_app = create_landscape(
            Landscape.load(),
            {LANDSCAPE_TOKEN_A: Grant("manager-a", frozenset({"crm", "stock", "pricing"})),
             LANDSCAPE_TOKEN_B: Grant("manager-b", frozenset({"crm", "stock", "pricing"}))},
        )
        landscape_http = TestClient(landscape_app, base_url="http://demo-landscape")
        holder: dict[str, Any] = {}
        workforce = WorkforceService(
            connection, proposals=proposals, journal=journal, drafter=drafter, validator=validator,
            tools=lambda harness_id: holder["researcher"].tools(harness_id), clock=clock,
        )
        researcher = ResearcherService(
            connection, harness=harness, proposals=proposals, journal=journal, validator=validator,
            registry=registry(), secret_store=SecretStore(secrets, vault), http_client=landscape_http,
            on_new_system=lambda identity, system: workforce.propose_tool_extension(identity, system=system),
            clock=clock,
        )
        holder["researcher"] = researcher
        connection.commit()
        env = Env(schema, connection, harness, proposals, journal, validator, drafter, workforce, researcher,
                  landscape_http, clock, vault)
        yield env
    finally:
        for extra in env.extra_connections if env else []:
            extra.close()
        connection.close()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def journal_kinds(env: Env, identity: Registration) -> list[str]:
    rows = env.connection.execute(
        "SELECT kind FROM journal_entries WHERE harness_id = %s ORDER BY created_at, id", (identity.harness_id,)
    ).fetchall()
    return [row[0] for row in rows]
