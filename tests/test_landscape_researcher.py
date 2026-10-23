"""T-19: Исследователь — подключение, драфт адаптера, применение, discovery (C-3; R-02, R-03, R-08)."""

import httpx
import pytest

from src.common.adapter_process import run_adapter
from src.harness.proposals import ConsentError
from src.landscape.bridge import make_bridge
from src.landscape.drafter import DraftRejected, draft_adapter, files_sha256
from src.landscape.registry import parse_registry
from src.validator.checkers import default_checkers
from src.validator.scorecard import ScoreCardSigner
from src.validator.service import Validator
from tests.support_harness import LANDSCAPE_TOKEN_A, harness_env, journal_kinds
from tests.test_workforce import active_twin


# --- драфт по шаблону проходит профиль Adapter (без БД) ------------------------------------

@pytest.mark.parametrize("system", ["crm", "stock", "pricing"])
def test_template_adapter_passes_the_whole_adapter_profile(system):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from src.demo_landscape.data import SCHEMAS

    artifact = draft_adapter(system=system, schema=SCHEMAS[system], secret_ref="env:CRM_TOKEN_A", author="researcher",
                             job_id="connect:x", created_at="2026-10-21T12:00:00+00:00",
                             test_data_ref="demo-landscape:fixture-v1")
    validator = Validator(ScoreCardSigner(Ed25519PrivateKey.generate()), default_checkers(), profile_set="demo-v1")
    card = validator.validate(artifact, "adapter", {"company_id": "00000000-0000-4000-8000-000000000001",
                                                    "harness_id": "00000000-0000-4000-8000-000000000002"})
    assert card.verdict == "pass", [(c.name, c.metrics, validator.log(c.log_ref)) for c in card.checks]
    assert artifact["manifest"]["writes"] == []


@pytest.mark.parametrize("schema", [
    {"system": "crm", "objects": []},
    {"system": "other", "objects": [{"name": "order", "key": "id", "path": "/orders"}]},
    {"system": "crm", "objects": [{"name": "order", "key": "id"}]},
    {"system": "crm", "objects": [{"name": "order", "key": "id", "path": "/../../etc"}]},
    {"system": "crm", "objects": [{"name": "Order\"); import os", "key": "id", "path": "/orders"}]},
])
def test_unusable_or_unsafe_schema_is_rejected(schema):
    with pytest.raises(DraftRejected):
        draft_adapter(system="crm", schema=schema, secret_ref="env:X", author="r", job_id="j",
                      created_at="t", test_data_ref="demo-landscape:fixture-v1")


def test_registry_parsing():
    entries = parse_registry('{"crm": {"base_url": "http://demo-landscape:8080/crm/", "test_data_ref": "demo-landscape:fixture-v1"}}')
    assert entries["crm"].base_url == "http://demo-landscape:8080/crm"
    assert parse_registry("") == {}
    for raw in ['[]', '{"crm": {"base_url": "ftp://x"}}', '{"c r m": {"base_url": "http://x", "test_data_ref": "a:b"}}']:
        with pytest.raises(ValueError):
            parse_registry(raw)


# --- мост: только GET внутри системы, токен у харнесса --------------------------------------

def test_bridge_refuses_paths_outside_the_system_and_never_follows_them():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    calls = []
    bridge = make_bridge(base_url="http://sys/crm", token="t0ken", client=httpx.Client(transport=httpx.MockTransport(handler)),
                         on_call=lambda path, status: calls.append((path, status)))
    assert bridge("/orders", {"status": "new"}) == (200, {"ok": True})
    assert requests[0].method == "GET" and requests[0].headers["Authorization"] == "Bearer t0ken"
    assert str(requests[0].url) == "http://sys/crm/orders?status=new"
    for bad in ("/../stock/items", "//evil.example/x", "orders", "/orders?x=1", "/a:b"):
        assert bridge(bad, {})[0] == 400
    assert len(requests) == 1
    assert calls[-1] == ("/a:b", 400)


ENV_LEAK = """
import json

class Adapter:
    def __init__(self, http):
        self.http = http
    def capabilities(self):
        return {"reads": [], "writes": []}
    def discover(self):
        try:
            self.http.post("/orders")
        except Exception:
            pass
        return {"system": "crm", "objects": [], "seen": self.http.get("/schema")}
    def probe(self, ref):
        return {}
"""


def test_adapter_in_bridge_mode_cannot_write_and_never_sees_the_token():
    seen = []
    run = run_adapter({"adapter.py": ENV_LEAK}, operations=["discover"],
                      bridge=lambda path, params: (seen.append(path), (200, {"path": path}))[1])
    assert run.reason == "write_attempt" and run.writes == ["POST /orders"]
    assert seen == ["/schema"]
    assert "t0ken" not in str(run.result) and "Bearer" not in str(run.result)


# --- T-19 на PostgreSQL и живом демо-ландшафте ------------------------------------------------

def connect_and_draft(env, me, system="crm"):
    proposal = env.connect(me, system=system)
    token = env.approve(me, proposal.id)
    return env.researcher.on_connect_approved(me, proposal_id=proposal.id, token=token)


def test_r_02_connect_requires_consent():
    with harness_env() as env:
        me = env.register()
        proposal = env.connect(me)
        with pytest.raises(ConsentError):
            env.researcher.on_connect_approved(me, proposal_id=proposal.id, token=None)
        assert env.researcher.tools(me.harness_id) == ([], [])


def test_connect_approval_drafts_an_adapter_that_passed_the_validator():
    with harness_env() as env:
        me = env.register()
        result = connect_and_draft(env, me)

        assert result["connected"] and result["adapter"] == "proposed"
        proposal = env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="adapter")[0]
        assert proposal.subject["system"] == "crm"
        assert proposal.subject["manifest"]["auth"]["secret_ref"] == "env:CRM_TOKEN_A"
        assert proposal.score_ref == proposal.subject["scorecard_id"]
        assert env.researcher.tools(me.harness_id) == ([], [])     # до согласия адаптер не действует (R-08)
        assert journal_kinds(env, me)[-2:] == ["landscape.connected", "adapter.proposed"]


def test_r_08_adapter_is_applied_only_with_consent_and_then_discovers_read_only():
    with harness_env() as env:
        me = env.register()
        result = connect_and_draft(env, me)
        with pytest.raises(ConsentError):
            env.researcher.on_adapter_approved(me, proposal_id=result["adapter_proposal_id"], token=None)

        token = env.approve(me, result["adapter_proposal_id"])
        applied = env.researcher.on_adapter_approved(me, proposal_id=result["adapter_proposal_id"], token=token)

        assert applied["discovery"]["ok"] is True
        order = applied["discovery"]["objects"][0]
        assert (order["name"], order["count"], order["sensitivity"]) == ("order", 2, {"client": "pii", "contact": "pii"})
        assert env.researcher.tools(me.harness_id) == (["crm"], ["order:read"])
        calls = env.connection.execute(
            "SELECT payload FROM journal_entries WHERE harness_id = %s AND kind = 'adapter.call' ORDER BY created_at",
            (me.harness_id,),
        ).fetchall()
        assert [(c[0]["method"], c[0]["path"], c[0]["status"]) for c in calls] == [
            ("GET", "/schema", 200), ("GET", "/orders", 200),
        ]   # R-03: только чтение
        map_rows = env.harness.list_harness_records(person_id=me.person_id, harness_id=me.harness_id, kind="landscape")
        assert map_rows[0].payload["system"] == "crm"


def test_adapter_changed_after_the_proposal_is_not_applied():
    with harness_env() as env:
        me = env.register()
        result = connect_and_draft(env, me)
        env.connection.execute(
            "UPDATE lc_adapters SET artifact = jsonb_set(artifact, '{files,adapter.py}', to_jsonb('class Adapter: pass'::text)) WHERE proposal_id = %s",
            (result["adapter_proposal_id"],),
        )
        token = env.approve(me, result["adapter_proposal_id"])
        with pytest.raises(ValueError, match="changed"):
            env.researcher.on_adapter_approved(me, proposal_id=result["adapter_proposal_id"], token=token)


def test_after_a_new_system_active_employees_get_a_passport_proposal_not_access():
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        result = connect_and_draft(env, me)
        token = env.approve(me, result["adapter_proposal_id"])
        applied = env.researcher.on_adapter_approved(me, proposal_id=result["adapter_proposal_id"], token=token)

        assert len(applied["passport_proposals"]) == 1
        twin = next(e for e in env.workforce.list(me) if e["employee_id"] == drafted["employee_id"])
        assert twin["active_version"] == 1          # доступ не расширен без согласия куратора


def test_unreachable_validator_rolls_nothing_forward():
    class DownValidator:
        def validate(self, *args):
            from src.validator.client import ValidatorUnavailable
            raise ValidatorUnavailable("down")

        def keys(self, refresh=False):
            return {}

    from src.landscape.service import LandscapeUnavailable

    with harness_env(validator=DownValidator()) as env:
        me = env.register()
        with pytest.raises(LandscapeUnavailable):
            connect_and_draft(env, me)


def test_files_hash_is_stable_and_order_independent():
    assert files_sha256({"a.py": "1", "b.py": "2"}) == files_sha256({"b.py": "2", "a.py": "1"})
    assert files_sha256({"a.py": "1"}) != files_sha256({"a.py": "2"})


def test_landscape_token_is_only_in_the_harness_secret_store():
    assert LANDSCAPE_TOKEN_A.startswith("demo-landscape-token")


def test_harness_wiring_is_off_without_validator_url():
    from src.harness.wiring import build_extensions

    assert build_extensions(connection=None, settings=None, secret_store=None, environ={}) == {}
