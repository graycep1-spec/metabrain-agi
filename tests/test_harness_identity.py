from collections.abc import Iterator
import os
from uuid import uuid4

import psycopg
import pytest

from src.harness.store import HarnessAccessDenied, HarnessStore


@pytest.fixture
def harness_store() -> Iterator[HarnessStore]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL contract tests")

    schema = f"test_harness_{uuid4().hex}"
    with psycopg.connect(database_url) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.commit()

    connection = psycopg.connect(
        database_url,
        options=f"-c search_path={schema}",
    )
    try:
        yield HarnessStore(connection)
    finally:
        connection.close()
        with psycopg.connect(database_url) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            admin.commit()


def test_registration_creates_company_person_harness_and_researcher(harness_store):
    identity = harness_store.register_person(
        channel="telegram",
        channel_user_id="telegram-user-1",
        company_name="Example company",
    )

    employees = harness_store.list_harness_records(
        person_id=identity.person_id,
        harness_id=identity.harness_id,
        kind="employee",
    )

    assert identity.person_id
    assert identity.company_id
    assert identity.harness_id
    assert len(employees) == 1
    assert employees[0].payload == {
        "kind": "researcher",
        "title": "Исследователь",
        "status": "active",
        "autonomy": "L0",
    }


@pytest.mark.parametrize(
    "kind",
    ["landscape.map", "employee", "ask", "journal"],
)
def test_gt_c1_06_foreign_harness_data_is_denied_at_storage_boundary(
    harness_store, kind
):
    owner = harness_store.register_person(
        channel="telegram",
        channel_user_id="telegram-user-owner",
        company_name="Shared company",
    )
    other_person = harness_store.register_person(
        channel="telegram",
        channel_user_id="telegram-user-other",
        trusted_company_id=owner.company_id,
    )
    private_record = harness_store.append_harness_record(
        person_id=owner.person_id,
        company_id=owner.company_id,
        harness_id=owner.harness_id,
        kind=kind,
        payload={"private": f"{kind}-owner-data"},
    )

    with pytest.raises(HarnessAccessDenied) as error:
        harness_store.list_harness_records(
            person_id=other_person.person_id,
            harness_id=owner.harness_id,
            kind=kind,
        )

    assert error.value.status_code == 403
    assert error.value.code == "forbidden_harness"
    assert private_record.payload["private"] not in str(error.value)
    with pytest.raises(HarnessAccessDenied):
        harness_store.append_harness_record(
            person_id=other_person.person_id,
            company_id=owner.company_id,
            harness_id=owner.harness_id,
            kind=kind,
            payload={"private": "unauthorized-write"},
        )


def test_harness_records_are_scoped_in_queries_and_accessible_to_their_owner(
    harness_store,
):
    identity = harness_store.register_person(
        channel="telegram",
        channel_user_id="telegram-user-scoped",
        company_name="Scoped company",
    )
    harness_store.append_harness_record(
        person_id=identity.person_id,
        company_id=identity.company_id,
        harness_id=identity.harness_id,
        kind="journal",
        payload={"event": "private-event"},
    )

    records = harness_store.list_harness_records(
        person_id=identity.person_id,
        harness_id=identity.harness_id,
        kind="journal",
    )

    assert [record.payload for record in records] == [
        {"event": "private-event"}
    ]
