"""Изолированный запуск адаптера (ADR-017, ADR-023). Только stdlib, из `src` ничего не импортирует.

Запуск: `python -I -S -B adapter_runner.py`. Первая строка stdin — JSON-запрос:
  {nonce, dir, mode: "stand" | "bridge", routes?, refs, operations}
- `stand`  — HTTP адаптера отвечает тест-стенд из `routes` (sandbox валидатора);
- `bridge` — каждый `get` уходит родителю строкой `BRIDGE <nonce> <json>` в stdout,
             ответ приходит следующей строкой stdin (харнесс выполняет GET со своим токеном).

Нарушения пишутся в stderr сразу: `SANDBOX_BLOCKED <событие>`, `SANDBOX_WRITE <метод> <путь>`.
Результат — последняя строка stdout, начинающаяся с nonce.
"""

import json
import os
import sys
from urllib.parse import parse_qsl, urlsplit


BLOCKED_PREFIXES = (
    "socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
    "os.fork", "os.forkpty", "os.startfile", "os.kill", "os.killpg", "os.putenv", "os.unsetenv",
    "os.remove", "os.rename", "os.replace", "os.rmdir", "os.mkdir", "os.symlink", "os.link",
    "os.chmod", "os.chown", "os.truncate", "os.chdir", "ctypes.", "shutil.", "urllib.Request",
    "http.client.", "ftplib.", "smtplib.", "poplib.", "imaplib.", "nntplib.", "telnetlib.",
    "webbrowser.", "winreg.", "msvcrt.", "_winapi.", "sys.addaudithook",
    "sys.settrace", "sys.setprofile", "pty.", "resource.setrlimit", "signal.",
)
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
OPERATIONS = ("capabilities", "discover", "probes")


def _report(line: str) -> None:
    os.write(2, (line + "\n").encode("utf-8", "replace"))


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(os.fsdecode(path)))


def _make_hook(allowed_read: list[str]):
    busy = [False]

    def inside(path) -> bool:
        target = _norm(path)
        return any(target == root or target.startswith(root + os.sep) for root in allowed_read)

    def violation(event: str, args) -> bool:
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, 0))[:3]
            if isinstance(path, int):
                return False
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                mode is None and isinstance(flags, int) and flags & WRITE_FLAGS
            )
            return bool(writing) or not inside(path)
        if event in ("os.listdir", "os.scandir"):
            path = args[0] if args else None
            return not inside("." if path is None else path)
        return event == "sys._getframe" or event.startswith(BLOCKED_PREFIXES)

    def hook(event, args):
        if busy[0]:
            return
        busy[0] = True
        try:
            blocked = violation(event, args)
        finally:
            busy[0] = False
        if blocked:
            _report(f"SANDBOX_BLOCKED {event}")
            raise PermissionError(f"sandbox: {event} is not allowed")

    return hook


def _split(path, params):
    parts = urlsplit(str(path))
    route = "/" + parts.path.lstrip("/")
    query = dict(parse_qsl(parts.query))
    query.update({str(k): str(v) for k, v in (params or {}).items()})
    return route, query


class _ReadOnly:
    def _write(self, method, path, *args, **kwargs):
        _report(f"SANDBOX_WRITE {method} {path}")
        raise PermissionError("read-only: writes are not allowed in the demo slice")

    def post(self, path, *args, **kwargs):
        return self._write("POST", path)

    def put(self, path, *args, **kwargs):
        return self._write("PUT", path)

    def patch(self, path, *args, **kwargs):
        return self._write("PATCH", path)

    def delete(self, path, *args, **kwargs):
        return self._write("DELETE", path)


class StandHTTP(_ReadOnly):
    """Клиент системы для адаптера (C-3): только GET, ответы — из тест-стенда."""

    def __init__(self, routes: dict):
        self._routes = routes

    def get(self, path, params=None):
        route, query = _split(path, params)
        if route not in self._routes:
            raise LookupError(f"404 Not Found: {route}")
        body = json.loads(json.dumps(self._routes[route]))  # копия: адаптер не портит стенд
        if query and isinstance(body, dict) and isinstance(body.get("items"), list):
            body["items"] = [
                item for item in body["items"]
                if all(str(item.get(k)) == v for k, v in query.items())
            ]
        return body


class BridgeHTTP(_ReadOnly):
    """Клиент системы для адаптера (C-3): GET выполняет родитель-харнесс."""

    def __init__(self, nonce: str, stdin):
        self._nonce = nonce
        self._stdin = stdin

    def get(self, path, params=None):
        route, query = _split(path, params)
        message = json.dumps({"path": route, "params": query}, ensure_ascii=False)
        os.write(1, f"BRIDGE {self._nonce} {message}\n".encode("utf-8"))
        line = self._stdin.readline()
        if not line:
            raise ConnectionError("bridge closed")
        answer = json.loads(line.decode("utf-8"))
        status = answer.get("status")
        if status == 200:
            return answer.get("body")
        if status == 404:
            raise LookupError(f"404 Not Found: {route}")
        if status in (401, 403):
            raise PermissionError(f"{status}: {route}")
        raise ConnectionError(f"{status}: {answer.get('error', 'bridge error')}")


def _call(fn, *args):
    try:
        return {"ok": True, "value": fn(*args)}
    except BaseException as error:  # noqa: BLE001 — любой сбой адаптера = результат прогона
        return {"ok": False, "error": f"{type(error).__name__}: {error}"[:500]}


def main() -> None:
    stdin = sys.stdin.buffer
    request = json.loads(stdin.readline().decode("utf-8"))
    nonce, workdir = request["nonce"], request["dir"]
    operations = [op for op in request.get("operations", OPERATIONS) if op in OPERATIONS]
    system_root = os.environ.get("SYSTEMROOT") if os.name == "nt" else None
    os.environ.clear()
    if system_root is not None:
        os.environ["SYSTEMROOT"] = system_root

    try:
        import resource

        limit = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass

    if request.get("mode") == "bridge":
        http = BridgeHTTP(nonce, stdin)
    else:
        http = StandHTTP(request.get("routes") or {})

    allowed = sorted({_norm(p) for p in (workdir, sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)})
    sys.dont_write_bytecode = True
    import importlib

    sys.addaudithook(_make_hook(allowed))
    sys.path.insert(0, workdir)

    result: dict = {}
    loaded = _call(lambda: importlib.import_module("adapter").Adapter(http))
    if not loaded["ok"]:
        result["error"] = loaded["error"]
    else:
        adapter = loaded["value"]
        if "capabilities" in operations:
            result["capabilities"] = _call(adapter.capabilities)
        if "discover" in operations:
            result["discover"] = _call(adapter.discover)
        if "probes" in operations:
            result["probes"] = {ref: _call(adapter.probe, ref) for ref in request.get("refs", [])}

    payload = json.dumps(result, default=str, ensure_ascii=False)
    os.write(1, (nonce + payload + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
