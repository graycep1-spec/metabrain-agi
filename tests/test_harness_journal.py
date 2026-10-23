from collections.abc import Iterator
from datetime import datetime, timezone
import os
from uuid import uuid4

import psycopg
import pytest

from src.harness.journal import JournalStore
from src.harness.store import HarnessAccessDenied, HarnessStore


@pytest.fixture
def journal_environment() -> Iterator[tuple[psycopg.Connection, HarnessStore, JournalStore]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL contract tests")

    schema = f"test_journal_{uuid4().hex}"
    with psycopg.connect(database_url) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.commit()

    connection = psycopg.connect(
        database_url,
        options=f"-c search_path={schema}",
    )
    try:
        harness_store = HarnessStore(connection)
        yield connection, harness_store, JournalStore(connection)
    finally:
        connection.close()
        with psycopg.connect(database_url) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            admin.commit()


def create_harness(harness_store: HarnessStore, suffix: str = "owner"):
    return harness_store.register_person(
        channel="telegram",
        channel_user_id=f"journal-user-{suffix}",
        company_name="Journal company",
    )


def test_journal_entries_are_append_only_even_for_direct_database_mutations(
    journal_environment,
):
    connection, harness_store, journal = journal_environment
    identity = create_harness(harness_store)
    entry = journal.append_entry(
        person_id=identity.person_id,
        company_id=identity.company_id,
        harness_id=identity.harness_id,
        kind="decision",
        payload={"decision": "approved"},
    )

    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
        with connection.transaction():
            connection.execute(
                "UPDATE journal_entries SET payload = '{}'::jsonb WHERE id = %s",
                (entry.id,),
            )

    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
        with connection.transaction():
            connection.execute(
                "DELETE FROM journal_entries WHERE id = %s",
                (entry.id,),
            )

    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
        with connection.transaction():
            connection.execute("TRUNCATE journal_entries")


def test_journal_listing_is_owner_scoped_and_preserves_provenance(
    journal_environment,
):
    _, harness_store, journal = journal_environment
    owner = create_harness(harness_store, "owner")
    same_company_other_person = harness_store.register_person(
        channel="telegram",
        channel_user_id="journal-user-other",
        trusted_company_id=owner.company_id,
    )
    entry = journal.append_entry(
        person_id=owner.person_id,
        company_id=owner.company_id,
        harness_id=owner.harness_id,
        employee_id=uuid4(),
        kind="llm.call",
        payload={"provider": "local", "cost": "0.003"},
        sources=[{"ref": "order:42"}],
        score_ref="score-123",
    )

    page = journal.list_entries(
        person_id=owner.person_id,
        harness_id=owner.harness_id,
        kind="llm.call",
    )

    assert page.entries == (entry,)
    assert page.entries[0].sources == [{"ref": "order:42"}]
    assert page.entries[0].score_ref == "score-123"
    assert page.entries[0].payload["provider"] == "local"
    with pytest.raises(HarnessAccessDenied):
        journal.list_entries(
            person_id=same_company_other_person.person_id,
            harness_id=owner.harness_id,
        )


def test_journal_filters_and_cursor_return_stable_pages(journal_environment):
    _, harness_store, journal = journal_environment
    identity = create_harness(harness_store)
    employee_id = uuid4()
    first = journal.append_entry(
        person_id=identity.person_id,
        company_id=identity.company_id,
        harness_id=identity.harness_id,
        employee_id=employee_id,
        kind="work",
        payload={"sequence": 1},
    )
    second = journal.append_entry(
        person_id=identity.person_id,
        company_id=identity.company_id,
        harness_id=identity.harness_id,
        employee_id=employee_id,
        kind="decision",
        payload={"sequence": 2},
    )
    third = journal.append_entry(
        person_id=identity.person_id,
        company_id=identity.company_id,
        harness_id=identity.harness_id,
        employee_id=uuid4(),
        kind="work",
        payload={"sequence": 3},
    )

    first_page = journal.list_entries(
        person_id=identity.person_id,
        harness_id=identity.harness_id,
        employee_id=employee_id,
        from_time=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_time=datetime(2100, 1, 1, tzinfo=timezone.utc),
        page_size=1,
    )
    second_page = journal.list_entries(
        person_id=identity.person_id,
        harness_id=identity.harness_id,
        employee_id=employee_id,
        kind="work",
        cursor=first_page.next_cursor,
        page_size=1,
    )

    assert first_page.entries[0].payload["sequence"] in {1, 2}
    assert first_page.next_cursor is not None
    assert second_page.entries[0].payload["sequence"] in {1, 2}
    assert first_page.entries[0].id != second_page.entries[0].id
    assert second_page.next_cursor is None
    page_ids = {entry.id for entry in first_page.entries + second_page.entries}
    assert page_ids == {first.id, second.id}
    assert third.id not in page_ids


def test_journal_rejects_malformed_cursor(journal_environment):
    _, harness_store, journal = journal_environment
    identity = create_harness(harness_store)

    with pytest.raises(ValueError, match="invalid journal cursor"):
        journal.list_entries(
            person_id=identity.person_id,
            harness_id=identity.harness_id,
            cursor="not-a-valid-cursor",
        )
