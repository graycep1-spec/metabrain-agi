from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import base64
import os
from uuid import uuid4

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from src.common.secrets import EnvSecretStore
from src.harness.journal import JournalStore
from src.harness.proposals import (
    ConsentError,
    ProposalConflict,
    ProposalStore,
)
from src.harness.store import HarnessStore


SIGNING_KEY_REF = "env:CONSENT_SIGNING_KEY"
KEY_MATERIAL = base64.b64encode(
    Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw,
        PrivateFormat.Raw,
        NoEncryption(),
    )
).decode("ascii")
ROOT_KEY = Ed25519PrivateKey.from_private_bytes(base64.b64decode(KEY_MATERIAL))
PUBLIC_KEY_MATERIAL = base64.b64encode(
    ROOT_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
).decode("ascii")


@pytest.fixture
def proposal_environment() -> Iterator[tuple[HarnessStore, ProposalStore]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL contract tests")

    schema = f"test_proposals_{uuid4().hex}"
    with psycopg.connect(database_url) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.commit()

    connection = psycopg.connect(
        database_url,
        options=f"-c search_path={schema}",
    )
    try:
        harness_store = HarnessStore(connection)
        yield harness_store, ProposalStore(
            connection,
            secret_store=EnvSecretStore(
                {
                    "CONSENT_SIGNING_KEY": KEY_MATERIAL,
                    "CONSENT_PUBLIC_KEY": PUBLIC_KEY_MATERIAL,
                }
            ),
            signing_key_ref=SIGNING_KEY_REF,
            token_ttl=timedelta(minutes=15),
        )
    finally:
        connection.close()
        with psycopg.connect(database_url) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            admin.commit()


def make_harness(harness_store: HarnessStore, suffix: str = "owner"):
    return harness_store.register_person(
        channel="telegram",
        channel_user_id=f"proposal-user-{suffix}",
        company_name="Proposal company",
    )


def make_proposal(proposals: ProposalStore, identity):
    return proposals.create_proposal(
        person_id=identity.person_id,
        company_id=identity.company_id,
        harness_id=identity.harness_id,
        kind="passport",
        summary="Update role passport",
        subject={
            "employee_id": "employee-1",
            "passport_patch": {"schedule": "weekdays"},
        },
        sources=[{"ref": "role-description-1"}],
        score_ref="score-passport-1",
    )


def test_consent_signatures_use_distinct_harness_keys_and_public_verification():
    proposals = object.__new__(ProposalStore)
    proposals._secret_store = EnvSecretStore(
        {
            "CONSENT_SIGNING_KEY": KEY_MATERIAL,
            "CONSENT_PUBLIC_KEY": PUBLIC_KEY_MATERIAL,
        }
    )
    proposals._signing_key_ref = SIGNING_KEY_REF
    proposals._verification_key_ref = "env:CONSENT_PUBLIC_KEY"
    proposals._token_ttl = timedelta(minutes=2)
    now = datetime.now(timezone.utc)
    tokens = [
        proposals._issue_token(
            harness_id=uuid4(),
            proposal_id=uuid4(),
            subject={"employee_id": f"employee-{index}"},
            scope="passport",
            issued_by=uuid4(),
            surface="telegram",
            issued_at=now,
        )
        for index in range(2)
    ]

    assert tokens[0].signing_public_key != tokens[1].signing_public_key
    assert all(proposals.verify_token_signature(token) for token in tokens)
    assert all(token.exp - token.issued_at == timedelta(minutes=2) for token in tokens)


def test_gt_c1_03_proposal_cannot_be_applied_without_consent(proposal_environment):
    harness_store, proposals = proposal_environment
    identity = make_harness(harness_store)
    proposal = make_proposal(proposals, identity)

    with pytest.raises(ConsentError) as error:
        proposals.apply_proposal(
            person_id=identity.person_id,
            harness_id=identity.harness_id,
            proposal_id=proposal.id,
            consent_token=None,
        )

    assert error.value.code == "consent_required"


def test_approval_issues_ed25519_signed_consent_bound_to_proposal_and_surface(
    proposal_environment,
):
    harness_store, proposals = proposal_environment
    identity = make_harness(harness_store)
    proposal = make_proposal(proposals, identity)

    result = proposals.decide(
        person_id=identity.person_id,
        harness_id=identity.harness_id,
        proposal_id=proposal.id,
        decision="approve",
        actor_person_id=identity.person_id,
        surface="telegram",
        channel_user_id="proposal-user-owner",
    )

    token = result.consent_token
    assert result.status == "approved"
    assert token.token_id
    assert token.harness_id == identity.harness_id
    assert token.proposal_id == proposal.id
    assert token.issued_by == identity.person_id
    assert token.surface == "telegram"
    assert token.issued_at < token.exp
    assert len(token.subject_hash) == 64
    assert token.signing_public_key
    assert token.key_certificate
    assert proposals.verify_token_signature(token)

    applied = proposals.apply_proposal(
        person_id=identity.person_id,
        harness_id=identity.harness_id,
        proposal_id=proposal.id,
        consent_token=token,
    )

    assert applied["employee_id"] == "employee-1"
    entries = JournalStore(proposals._connection).list_entries(
        person_id=identity.person_id,
        harness_id=identity.harness_id,
        kind="proposal.applied",
    ).entries
    assert len(entries) == 1
    assert entries[0].payload == {
        "proposal_id": str(proposal.id),
        "token_id": str(token.token_id),
        "scope": token.scope,
    }


def test_consent_signing_key_is_distinct_per_harness(proposal_environment):
    harness_store, proposals = proposal_environment
    tokens = []
    for suffix in ("first", "second"):
        identity = make_harness(harness_store, suffix)
        proposal = make_proposal(proposals, identity)
        tokens.append(
            proposals.decide(
                person_id=identity.person_id,
                harness_id=identity.harness_id,
                proposal_id=proposal.id,
                decision="approve",
                actor_person_id=identity.person_id,
                surface="telegram",
                channel_user_id=f"proposal-user-{suffix}",
            ).consent_token
        )

    assert tokens[0].signing_public_key != tokens[1].signing_public_key
    assert all(proposals.verify_token_signature(token) for token in tokens)


@pytest.mark.parametrize(
    ("tamper", "expected_code"),
    [
        ("signature", "invalid_signature"),
        ("subject", "subject_changed"),
        ("expiry", "consent_expired"),
        ("harness", "forbidden_harness"),
    ],
)
def test_gt_c1_05_modified_expired_or_foreign_consent_is_rejected(
    proposal_environment, tamper, expected_code
):
    harness_store, proposals = proposal_environment
    identity = make_harness(harness_store)
    proposal = make_proposal(proposals, identity)
    token = proposals.decide(
        person_id=identity.person_id,
        harness_id=identity.harness_id,
        proposal_id=proposal.id,
        decision="approve",
        actor_person_id=identity.person_id,
        surface="telegram",
        channel_user_id="proposal-user-owner",
    ).consent_token

    if tamper == "signature":
        token = replace(token, signature="invalid")
    elif tamper == "subject":
        altered_subject = {
            "employee_id": "employee-1",
            "passport_patch": {"schedule": "daily"},
        }
        with pytest.raises(ConsentError) as error:
            proposals.apply_proposal(
                person_id=identity.person_id,
                harness_id=identity.harness_id,
                proposal_id=proposal.id,
                consent_token=token,
                current_subject=altered_subject,
            )
        assert error.value.code == expected_code
        return
    elif tamper == "expiry":
        proposals._clock = lambda: token.exp + timedelta(seconds=1)
    elif tamper == "harness":
        other = make_harness(harness_store, "other")
        with pytest.raises(ConsentError) as error:
            proposals.apply_proposal(
                person_id=other.person_id,
                harness_id=other.harness_id,
                proposal_id=proposal.id,
                consent_token=token,
            )
        assert error.value.code == expected_code
        return

    with pytest.raises(ConsentError) as error:
        proposals.apply_proposal(
            person_id=identity.person_id,
            harness_id=identity.harness_id,
            proposal_id=proposal.id,
            consent_token=token,
        )

    assert error.value.code == expected_code


def test_gt_c1_05_consent_token_is_one_time_only(proposal_environment):
    harness_store, proposals = proposal_environment
    identity = make_harness(harness_store)
    proposal = make_proposal(proposals, identity)
    token = proposals.decide(
        person_id=identity.person_id,
        harness_id=identity.harness_id,
        proposal_id=proposal.id,
        decision="approve",
        actor_person_id=identity.person_id,
        surface="telegram",
        channel_user_id="proposal-user-owner",
    ).consent_token

    proposals.apply_proposal(
        person_id=identity.person_id,
        harness_id=identity.harness_id,
        proposal_id=proposal.id,
        consent_token=token,
    )

    with pytest.raises(ConsentError) as error:
        proposals.apply_proposal(
            person_id=identity.person_id,
            harness_id=identity.harness_id,
            proposal_id=proposal.id,
            consent_token=token,
        )

    assert error.value.code == "consent_already_used"


def test_gt_c1_04_only_harness_curator_can_decide_and_second_decision_is_409(
    proposal_environment,
):
    harness_store, proposals = proposal_environment
    owner = make_harness(harness_store, "owner")
    other = harness_store.register_person(
        channel="telegram",
        channel_user_id="proposal-user-other",
        trusted_company_id=owner.company_id,
    )
    proposal = make_proposal(proposals, owner)

    with pytest.raises(ConsentError) as error:
        proposals.decide(
            person_id=other.person_id,
            harness_id=owner.harness_id,
            proposal_id=proposal.id,
            decision="approve",
            actor_person_id=other.person_id,
            surface="telegram",
            channel_user_id="proposal-user-other",
        )

    assert error.value.code == "not_curator"
    assert proposals.decide(
        person_id=owner.person_id,
        harness_id=owner.harness_id,
        proposal_id=proposal.id,
        decision="return",
        actor_person_id=owner.person_id,
        surface="telegram",
        channel_user_id="proposal-user-owner",
    ).status == "returned"

    with pytest.raises(ProposalConflict) as conflict:
        proposals.decide(
            person_id=owner.person_id,
            harness_id=owner.harness_id,
            proposal_id=proposal.id,
            decision="approve",
            actor_person_id=owner.person_id,
            surface="web",
        )

    assert conflict.value.status_code == 409
    assert conflict.value.code == "already_decided"
