"""Модуль установщиков (install.sh / install.ps1): .env, перенос ключа, проверки DeepSeek и бота без утечки секретов."""

import io
import json
from pathlib import Path
import sys

import httpx
import pytest

from src.common.installer import InstallError, check_bot, check_llm, main, parse, set_value


SECRET = "sk-test-0123456789abcdef"


def run(argv, capsys, stdin: str | None = None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out + out.err


def test_env_files_of_other_services_are_parsed():
    text = "\n".join(["# comment", "export DEEPSEEK_API_KEY='sk-a'", 'OTHER="x y"', "PLAIN=z", "broken line"])
    assert parse(text) == {"DEEPSEEK_API_KEY": "sk-a", "OTHER": "x y", "PLAIN": "z"}
    assert set_value("A=1\nB=2\n", "B", "new") == "A=1\nB='new'\n"
    assert set_value("A=1\n", "C", "3") == "A=1\nC='3'\n"
    with pytest.raises(InstallError):
        set_value("", "A", "it's")


def test_init_set_import_show_never_print_secrets(tmp_path, capsys, monkeypatch):
    env = tmp_path / ".env"
    assert run(["init", "--env", str(env), "--template", ".env.example"], capsys)[0] == 0
    first = env.read_text(encoding="utf-8")
    assert run(["init", "--env", str(env), "--template", ".env.example"], capsys)[0] == 0
    assert env.read_text(encoding="utf-8") == first                                 # повторная установка не перезаписывает

    code, output = run(["set", "TELEGRAM_BOT_TOKEN", "--env", str(env)], capsys, f"{SECRET}\n", monkeypatch)
    assert code == 0 and SECRET not in output
    other = tmp_path / "tsn.env"
    other.write_text(f"N8N_HOST=x\nDEEPSEEK_API_KEY=\"{SECRET}\"\n", encoding="utf-8")
    code, output = run(["import", "DEEPSEEK_API_KEY", "--from", str(other), "--env", str(env)], capsys)
    assert code == 0 and SECRET not in output

    values = parse(env.read_text(encoding="utf-8"))
    assert values["TELEGRAM_BOT_TOKEN"] == SECRET and values["DEEPSEEK_API_KEY"] == SECRET
    code, output = run(["show", "--env", str(env)], capsys)
    assert "DEEPSEEK_API_KEY: задан" in output and "WEB_BASE_URL: http://localhost:8081" in output and SECRET not in output

    assert run(["import", "MISSING_KEY", "--from", str(other), "--env", str(env)], capsys)[0] == 2
    code, output = run(["tokens", "--env", str(env)], capsys)
    grants = json.loads(values["DEMO_LANDSCAPE_TOKENS"])
    assert code == 0 and all(token in output for token in grants)


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_llm_reports_bad_key_and_missing_policy_models():
    def models(*ids, status=200):
        return lambda request: httpx.Response(status, json={"data": [{"id": i} for i in ids]})

    values = {"DEEPSEEK_API_KEY": SECRET}
    assert check_llm({}, client(models())) == ["DEEPSEEK_API_KEY не задан"]
    assert check_llm(values, client(models(status=401))) == ["DeepSeek не принял ключ (401)"]
    assert check_llm(values, client(models("deepseek-flash", "deepseek-v4-pro"))) == []
    problems = check_llm(values, client(models("deepseek-flash")))
    assert problems == ["в DeepSeek нет моделей из политики: deepseek-v4-pro"]


def test_check_bot_refuses_a_bot_that_already_has_a_webhook():
    def telegram(webhook: str):
        def handler(request):
            if request.url.path.endswith("/getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"username": "metabrain_demo_bot"}})
            return httpx.Response(200, json={"ok": True, "result": {"url": webhook}})
        return handler

    values = {"TELEGRAM_BOT_TOKEN": SECRET}
    assert check_bot(values, client(telegram(""))) == ("metabrain_demo_bot", [])
    username, problems = check_bot(values, client(telegram("https://tsn.example/webhook")))
    assert username == "metabrain_demo_bot" and "вебхук" in problems[0]
    assert check_bot(values, client(lambda r: httpx.Response(401, json={"ok": False})))[1] == ["Telegram не принял токен бота"]


def test_installers_and_compose_are_consistent():
    compose = Path("deploy/compose.yml").read_text(encoding="utf-8")
    assert "name: metabrain" in compose and "127.0.0.1" in compose and "qdrant" not in compose
    assert Path("install.sh").read_bytes().count(b"\r\n") == 0                         # bash не прочтёт CRLF
    assert Path("install.ps1").read_bytes().startswith(b"\xef\xbb\xbf")                # PowerShell 5.1 и кириллица
    assert "python -m src.common.installer" in Path("install.sh").read_text(encoding="utf-8")
