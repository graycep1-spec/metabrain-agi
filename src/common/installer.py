"""Логика установщиков `install.sh` / `install.ps1`: `.env`, ключи, проверки. Работает внутри образа MetaBrain.

Хосту нужен только Docker: скрипты установки запускают этот модуль в контейнере с каталогом установки в `/w`.
Секреты читаются из stdin или из смонтированного файла и никогда не печатаются.

    python -m src.common.installer init  --env deploy/.env [--template .env.example]
    python -m src.common.installer set   --env deploy/.env KEY            # значение — из stdin
    python -m src.common.installer import --env deploy/.env --from FILE KEY [--as NAME]
    python -m src.common.installer show  --env deploy/.env                # какие ключи заданы, без значений
    python -m src.common.installer check-llm --env deploy/.env           # ключ DeepSeek и модели политики
    python -m src.common.installer check-bot --env deploy/.env           # бот: getMe, нет ли чужого вебхука
    python -m src.common.installer tokens --env deploy/.env              # токены менеджеров демо (для администратора)
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys

import httpx

from src.common.demo_env import fill, generated


# Что установщик спрашивает у человека; остальное генерирует `init`.
ASKED = ("DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_USERNAME", "WEB_BASE_URL",
         "TELEGRAM_OIDC_CLIENT_ID", "TELEGRAM_OIDC_CLIENT_SECRET", "DEEPSEEK_API_BASE")
SECRET = {"DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OIDC_CLIENT_SECRET"}
LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class InstallError(RuntimeError):
    pass


def parse(text: str) -> dict[str, str]:
    """KEY=value, KEY='value', KEY="value", export KEY=value; комментарии и пустые строки пропускаются."""
    values = {}
    for line in text.splitlines():
        match = LINE.match(line.strip())
        if not match:
            continue
        raw = match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        values[match.group(1)] = raw
    return values


def set_value(text: str, name: str, value: str) -> str:
    if "'" in value or "\n" in value or "\r" in value:
        raise InstallError(f"{name}: значение не должно содержать кавычку ' или перевод строки")
    line = f"{name}='{value}'"
    lines = text.splitlines()
    for index, current in enumerate(lines):
        match = LINE.match(current.strip())
        if match and match.group(1) == name:
            lines[index] = line
            return "\n".join(lines) + "\n"
    return "\n".join(lines + [line]) + "\n"


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise InstallError(f"{path} не найден — сначала init")
    return parse(path.read_text(encoding="utf-8"))


def write_env(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows-том: права задаёт хост


# --- команды ---------------------------------------------------------------------------------

def cmd_init(args) -> int:
    path = Path(args.env)
    if path.exists():
        print(f"{path}: уже есть — оставляю как есть")
        return 0
    values, _ = generated()
    write_env(path, fill(Path(args.template).read_text(encoding="utf-8"), values))
    print(f"{path}: создан — ключи подписи, пароли БД, ключ хранилища, токены демо")
    return 0


def cmd_set(args) -> int:
    path = Path(args.env)
    value = sys.stdin.readline().strip()
    if not value:
        print(f"{args.name}: пусто — не меняю")
        return 0
    write_env(path, set_value(path.read_text(encoding="utf-8"), args.name, value))
    print(f"{args.name}: записано")
    return 0


def cmd_import(args) -> int:
    source = parse(Path(args.source).read_text(encoding="utf-8", errors="replace"))
    value = source.get(args.name, "").strip()
    if not value:
        raise InstallError(f"в {args.source} нет {args.name}")
    path = Path(args.env)
    target = args.target or args.name
    write_env(path, set_value(path.read_text(encoding="utf-8"), target, value))
    print(f"{target}: перенесено из {args.source}")
    return 0


def cmd_show(args) -> int:
    values = read_env(Path(args.env))
    for name in ASKED:
        value = values.get(name, "")
        shown = ("задан" if value else "пусто") if name in SECRET else (value or "пусто")
        print(f"{name}: {shown}")
    return 0


def check_llm(values: dict[str, str], client: httpx.Client) -> list[str]:
    """Пустой список — всё в порядке; иначе — что не так. Запрос списка моделей бесплатный."""
    from src.metabrain.policy import load_policy

    key = values.get("DEEPSEEK_API_KEY", "")
    if not key:
        return ["DEEPSEEK_API_KEY не задан"]
    base = (values.get("DEEPSEEK_API_BASE") or "https://api.deepseek.com").rstrip("/")
    try:
        response = client.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as error:
        return [f"DeepSeek недоступен: {type(error).__name__}"]
    if response.status_code == 401:
        return ["DeepSeek не принял ключ (401)"]
    if response.status_code != 200:
        return [f"DeepSeek ответил {response.status_code}"]
    available = {m.get("id") for m in response.json().get("data", []) if isinstance(m, dict)}
    wanted = {spec["model"] for spec in load_policy()["llm_routing"]["providers"].values()}
    missing = sorted(wanted - available)
    return [f"в DeepSeek нет моделей из политики: {', '.join(missing)}"] if missing else []


def check_bot(values: dict[str, str], client: httpx.Client) -> tuple[str | None, list[str]]:
    """(username, проблемы). Бот с вебхуком — чужой (например, бот на n8n): его не трогаем."""
    token = values.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return None, ["TELEGRAM_BOT_TOKEN не задан"]
    try:
        me = client.get(f"https://api.telegram.org/bot{token}/getMe")
        hook = client.get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
    except httpx.HTTPError as error:
        return None, [f"Telegram недоступен: {type(error).__name__}"]
    if me.status_code != 200 or not me.json().get("ok"):
        return None, ["Telegram не принял токен бота"]
    username = me.json()["result"].get("username")
    if hook.status_code == 200 and hook.json().get("result", {}).get("url"):
        return username, [f"у бота @{username} стоит вебхук — это другой работающий бот; создайте для MetaBrain отдельного"]
    return username, []


def cmd_check_llm(args) -> int:
    with httpx.Client(timeout=20) as client:
        problems = check_llm(read_env(Path(args.env)), client)
    print("\n".join(problems) or "DeepSeek: ключ принят, модели политики доступны")
    return 1 if problems else 0


def cmd_check_bot(args) -> int:
    path = Path(args.env)
    with httpx.Client(timeout=20) as client:
        username, problems = check_bot(read_env(path), client)
    if username and not problems:
        write_env(path, set_value(path.read_text(encoding="utf-8"), "TELEGRAM_BOT_USERNAME", username))
        print(f"Бот: @{username}, вебхука нет — MetaBrain будет его опрашивать")
        return 0
    print("\n".join(problems))
    return 1


def cmd_tokens(args) -> int:
    grants = json.loads(read_env(Path(args.env)).get("DEMO_LANDSCAPE_TOKENS") or "{}")
    for token, grant in grants.items():
        print(f"{grant.get('user')}: {token}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.common.installer")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("init", cmd_init), ("show", cmd_show), ("check-llm", cmd_check_llm),
                          ("check-bot", cmd_check_bot), ("tokens", cmd_tokens), ("set", cmd_set), ("import", cmd_import)):
        command = commands.add_parser(name)
        command.add_argument("--env", default="deploy/.env")
        command.set_defaults(handler=handler)
        if name == "init":
            command.add_argument("--template", default=".env.example")
        if name in ("set", "import"):
            command.add_argument("name")
        if name == "import":
            command.add_argument("--from", dest="source", required=True)
            command.add_argument("--as", dest="target")
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except InstallError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
