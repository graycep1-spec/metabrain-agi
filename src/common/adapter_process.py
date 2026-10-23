"""Родительская сторона изолированного запуска адаптера (ADR-017, ADR-023).

Используется валидатором (sandbox на тест-стенде) и харнессом (живая система через мост).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4


RUNNER = Path(__file__).with_name("adapter_runner.py")
OPERATIONS = ("capabilities", "discover", "probes")
DEFAULT_TIMEOUT = 10.0
MAX_OUTPUT_BYTES = 1_000_000
MAX_BRIDGE_CALLS = 200

# Мост: (путь относительно системы, параметры) -> (HTTP-статус, тело).
Bridge = Callable[[str, dict[str, str]], tuple[int, Any]]


@dataclass
class AdapterRun:
    ok: bool
    reason: str = ""
    blocked: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    log: str = ""
    bridge_calls: int = 0


def _pump(stream, sink: queue.Queue) -> None:
    for line in iter(stream.readline, b""):
        sink.put(line)
    sink.put(None)


def run_adapter(
    files: Mapping[str, str],
    *,
    refs: Sequence[str] = (),
    operations: Sequence[str] = OPERATIONS,
    routes: Mapping[str, Any] | None = None,
    bridge: Bridge | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> AdapterRun:
    """Исполнить адаптер без сети и секретов. Статический разбор здесь не делается."""
    nonce = uuid4().hex
    env = {"SYSTEMROOT": os.environ["SYSTEMROOT"]} if os.name == "nt" and "SYSTEMROOT" in os.environ else {}
    request = {
        "nonce": nonce,
        "mode": "bridge" if bridge else "stand",
        "routes": dict(routes or {}),
        "refs": list(refs),
        "operations": list(operations),
    }
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="adapter-run-") as workdir:
        for name, content in files.items():
            (Path(workdir) / name).write_text(content, encoding="utf-8")
        request["dir"] = workdir
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(RUNNER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=workdir, env=env,
        )
        out_lines: queue.Queue = queue.Queue()
        err_lines: queue.Queue = queue.Queue()
        threads = [
            threading.Thread(target=_pump, args=(process.stdout, out_lines), daemon=True),
            threading.Thread(target=_pump, args=(process.stderr, err_lines), daemon=True),
        ]
        for thread in threads:
            thread.start()

        final: str | None = None
        reason = ""
        bridge_calls = 0
        received = 0
        try:
            process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            process.stdin.flush()
            if bridge is None:
                process.stdin.close()
            prefix = f"BRIDGE {nonce} "
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    reason = "timeout"
                    break
                try:
                    raw = out_lines.get(timeout=remaining)
                except queue.Empty:
                    reason = "timeout"
                    break
                if raw is None:
                    break
                received += len(raw)
                if received > MAX_OUTPUT_BYTES:
                    reason = "output_too_large"
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(nonce):
                    final = line[len(nonce):]
                    break
                if bridge is not None and line.startswith(prefix):
                    bridge_calls += 1
                    if bridge_calls > MAX_BRIDGE_CALLS:
                        reason = "too_many_calls"
                        break
                    answer = _serve(bridge, line[len(prefix):])
                    process.stdin.write((json.dumps(answer, ensure_ascii=False) + "\n").encode("utf-8"))
                    process.stdin.flush()
                # остальные строки stdout — шум адаптера, игнорируются
        except (BrokenPipeError, OSError):
            reason = reason or "crashed"
        finally:
            if reason or final is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            for thread in threads:
                thread.join(timeout=2)

    stderr = b"".join(iter_queue(err_lines)).decode("utf-8", "replace")
    blocked = [ln.split(" ", 1)[1] for ln in stderr.splitlines() if ln.startswith("SANDBOX_BLOCKED ")]
    writes = [ln.split(" ", 1)[1] for ln in stderr.splitlines() if ln.startswith("SANDBOX_WRITE ")]
    run = AdapterRun(
        False, reason, blocked=blocked, writes=writes,
        duration_ms=int((time.monotonic() - started) * 1000), log=stderr[-4000:], bridge_calls=bridge_calls,
    )
    if reason:
        return run
    if final is None or process.returncode != 0:
        run.reason = "crashed"
        return run
    try:
        run.result = json.loads(final)
    except ValueError:
        run.reason = "bad_output"
        return run
    if blocked:
        run.reason = "blocked_operation"
    elif writes:
        run.reason = "write_attempt"
    elif "error" in run.result:
        run.reason = "adapter_error"
        run.log = run.result["error"]
    else:
        run.ok = True
    return run


def iter_queue(items: queue.Queue):
    while True:
        try:
            item = items.get_nowait()
        except queue.Empty:
            return
        if item is not None:
            yield item


def _serve(bridge: Bridge, raw: str) -> dict[str, Any]:
    try:
        message = json.loads(raw)
        path, params = message["path"], message.get("params") or {}
        if not isinstance(path, str) or not isinstance(params, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return {"status": 400, "error": "malformed bridge request"}
    try:
        status, body = bridge(path, {str(k): str(v) for k, v in params.items()})
    except Exception as error:  # сбой харнесса или системы — адаптеру как 502
        return {"status": 502, "error": type(error).__name__}
    return {"status": status, "body": body} if status == 200 else {"status": status}
