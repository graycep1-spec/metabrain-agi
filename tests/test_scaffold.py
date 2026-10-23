import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from src.stubs.server import create_server


ROOT = Path(__file__).resolve().parents[1]
APP_SERVICES = {"harness", "web", "validator", "metabrain", "metabrain-mcp", "demo-landscape"}
INFRA_SERVICES = {"postgres", "qdrant", "nats"}
IMAGES = {"metabrain-mcp": "metabrain"}  # MCP — тот же образ, что мета-мозг (ADR-022)


def test_compose_declares_application_and_infrastructure_services():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    assert set(compose["services"]) == APP_SERVICES | INFRA_SERVICES


def test_application_services_use_the_shared_stub_image_and_healthcheck():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    for name in APP_SERVICES:
        service = compose["services"][name]
        assert service["build"]["dockerfile"] == "Dockerfile.stub"
        assert service["image"] == f"metabrain/{IMAGES.get(name, name)}:local"
        assert service["environment"]["SERVICE_NAME"] == name
        assert service["healthcheck"]["test"]


def test_compose_preserves_expected_ports_dependencies_and_postgres_volume():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["harness"]["ports"] == ["127.0.0.1:8080:8080"]
    assert services["web"]["ports"] == ["127.0.0.1:8081:8080"]
    assert set(services["harness"]["depends_on"]) == {
        "validator",
        "metabrain",
        "postgres",
        "nats",
    }
    assert (
        services["harness"]["depends_on"]["postgres"]["condition"]
        == "service_healthy"
    )
    assert services["web"]["depends_on"] == ["harness"]
    assert services["postgres"]["volumes"] == [
        "pgdata:/var/lib/postgresql/data",
        "./deploy/postgres/init:/docker-entrypoint-initdb.d:ro",
    ]
    assert services["harness"]["command"] == ["python", "-m", "src.harness.app"]
    assert "HARNESS_DATABASE_URL" in services["harness"]["environment"]
    assert "HARNESS_API_TOKEN" in services["harness"]["environment"]
    assert "postgres" in services["harness"]["depends_on"]
    assert "pgdata" in compose["volumes"]
    assert "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}" == services[
        "postgres"
    ]["environment"]["POSTGRES_PASSWORD"]


def test_compose_scopes_credentials_to_services_that_need_them():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert "VALIDATOR_SIGNING_KEY" in services["validator"]["environment"]
    assert "VALIDATOR_SIGNING_KEY" not in services["harness"]["environment"]
    assert "CONSENT_SIGNING_KEY" in services["harness"]["environment"]
    assert "CONSENT_SIGNING_KEY" not in services["validator"]["environment"]
    assert "TELEGRAM_BOT_TOKEN" in services["web"]["environment"]
    assert "DEEPSEEK_API_KEY" in services["harness"]["environment"]
    assert "DEEPSEEK_API_KEY" not in services["web"]["environment"]
    assert "LOCAL_LLM_API_KEY" in services["harness"]["environment"]
    assert "POSTGRES_PASSWORD" in services["postgres"]["environment"]
    assert "POSTGRES_PASSWORD" not in services["harness"]["environment"]
    assert "HARNESS_DATABASE_URL" in services["harness"]["environment"]
    assert "HARNESS_API_TOKEN" in services["harness"]["environment"]
    assert services["harness"]["environment"]["HARNESS_DATABASE_URL"].startswith(
        "postgresql://harness:${HARNESS_DB_PASSWORD"
    )
    assert "POSTGRES_PASSWORD" not in services["harness"]["environment"]["HARNESS_DATABASE_URL"]
    assert "HARNESS_DB_PASSWORD" in services["postgres"]["environment"]
    assert "HARNESS_DB_PASSWORD" not in services["harness"]["environment"]
    assert "POSTGRES_PASSWORD" not in services["metabrain"]["environment"]


def test_local_environment_secrets_are_excluded_from_git_and_docker_build_context():
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_stub_health_endpoint_reports_its_service_name():
    server = create_server("validator")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/healthz", timeout=2
        ) as response:
            payload = json.load(response)

        assert payload == {"status": "ok", "service": "validator"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_stub_returns_not_found_for_unknown_paths():
    server = create_server("harness")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/unknown", timeout=2
            )
        except urllib.error.HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("unknown paths must return HTTP 404")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_service_database_roles_do_not_receive_the_superuser_password():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    metabrain = compose["services"]["metabrain"]["environment"]

    assert metabrain["METABRAIN_DATABASE_URL"].startswith("postgresql://metabrain:${METABRAIN_DB_PASSWORD")
    assert "POSTGRES_PASSWORD" not in metabrain["METABRAIN_DATABASE_URL"]
    assert (ROOT / "deploy/postgres/init/10-service-roles.sh").is_file()


def test_internal_metabrain_api_is_not_published_only_mcp_is():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert "ports" not in services["metabrain"]
    assert services["metabrain-mcp"]["ports"] == ["127.0.0.1:8082:8080"]
    assert services["metabrain-mcp"]["command"] == ["python", "-m", "src.metabrain.mcp"]
