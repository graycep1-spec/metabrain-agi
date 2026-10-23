"""Сквозной путь через C-1 HTTP API: /role → паспорт → согласие → подключение → адаптер → карта (T-13, T-19)."""

from fastapi.testclient import TestClient

from src.common.secrets import EnvSecretStore
from src.harness.api import create_app
from tests.support_harness import CONSENT_KEY, CONSENT_PUBLIC_KEY, harness_env


API_TOKEN = "test-harness-api-token-workforce"
USER = "555000"


def client_for(env) -> TestClient:
    app = create_app(
        env.connection, api_token=API_TOKEN, app_base_url="https://cabinet.example.test",
        secret_store=EnvSecretStore(
            {"CONSENT_SIGNING_KEY": CONSENT_KEY, "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY}
        ),
        workforce=env.workforce, researcher=env.researcher,
    )
    return TestClient(app)


def call(client, name, **payload):
    response = client.post(f"/v1/operations/{name}", json={"channel_user_id": USER, **payload},
                           headers={"Authorization": f"Bearer {API_TOKEN}"})
    return response.status_code, response.json()


def approve(client, proposal_id):
    return call(client, "proposal.decide", proposal_id=proposal_id, decision="approve", surface="telegram")


def test_role_to_twin_to_connected_crm_through_the_c1_api():
    with harness_env() as env:
        client = client_for(env)
        status, _ = call(client, "person.register", channel="telegram", company_name="ООО Пример",
                         external_llm_consent=True)
        assert status == 201

        status, drafted = call(client, "employee.draft", kind="twin", role_text="Я менеджер по продажам, готовлю КП")
        assert status == 201 and drafted["status"] == "pending"
        status, team = call(client, "employee.list")
        assert sorted((e["kind"], e["status"]) for e in team["employees"]) == [("researcher", "active"), ("twin", "draft")]

        status, decided = approve(client, drafted["proposal_id"])
        assert status == 200 and decided["applied"] == {"employee_id": drafted["employee_id"], "version": 1, "status": "active"}

        status, connect = call(client, "landscape.connect", system="crm", secret_ref="env:CRM_TOKEN_A")
        assert status == 201
        status, decided = approve(client, connect["proposal_id"])
        assert status == 200 and decided["applied"]["adapter"] == "proposed"

        status, decided = approve(client, decided["applied"]["adapter_proposal_id"])
        applied = decided["applied"]
        assert status == 200 and applied["discovery"]["ok"] is True and len(applied["passport_proposals"]) == 1

        status, landscape = call(client, "landscape.map")
        assert status == 200 and landscape["map"]["crm"]["objects"][0]["name"] == "order"

        status, pending = call(client, "proposal.list", status="pending", kind="passport")
        assert [p["payload"]["passport"]["tools"]["connectors"] for p in pending["proposals"]] == [["crm"]]


def test_rejected_passport_is_422_with_reasons_through_the_api():
    class RejectingValidator:
        def validate(self, artifact, profile, context):
            return {"scorecard_id": "sc", "verdict": "fail",
                    "checks": [{"name": "schema", "verdict": "fail", "metrics": {"errors": 1}}]}

        def keys(self, refresh=False):
            return {}

    with harness_env(validator=RejectingValidator()) as env:
        client = client_for(env)
        call(client, "person.register", channel="telegram", company_name="ООО Пример", external_llm_consent=True)
        status, result = call(client, "employee.draft", kind="twin", role_text="Я менеджер")
        assert status == 422 and result["status"] == "rejected" and result["reasons"]
        status, journal = call(client, "journal.list", kind="employee.passport.rejected")
        assert len(journal["entries"]) == 1           # причины остались в журнале, хоть паспорта и нет
