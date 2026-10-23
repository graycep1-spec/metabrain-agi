"""Профиль Adapter: schema, security, sandbox, eval (C-4; среда адаптера — C-3)."""

import ast
from collections import OrderedDict
from collections.abc import Mapping
import json
from pathlib import Path
import re
import threading
from typing import Any

from src.common.adapter_process import AdapterRun as SandboxRun, run_adapter
from src.validator.checks.detectors import find_secrets
from src.validator.checks.shape import NonEmpty, errors
from src.validator.checks.stand import Stand, load_stand
from src.validator.scorecard import Context, artifact_hash
from src.validator.service import CheckOutcome


SANDBOX_TIMEOUT = 10.0
MAX_FILES_CHARS = 200_000

FILE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.(py|json|md)$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
# Форматы secret store: env (ADR-014) и хранилище харнесса (ADR-028).
SECRET_REF = re.compile(r"^(env:[A-Z][A-Z0-9_]*|vault:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
SECRET_NAME = re.compile(r"(?i)(token|secret|passw|api_?key|private_?key|credential|auth)")

ALLOWED_IMPORTS = frozenset({
    "json", "re", "datetime", "dataclasses", "typing", "collections", "itertools",
    "functools", "math", "decimal", "enum", "string", "urllib.parse",
})
BANNED_CALLS = frozenset({
    "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars",
    "breakpoint", "input", "exit", "quit", "help",
})
ATTR_FUNCS = frozenset({"getattr", "setattr", "delattr", "hasattr"})

MANIFEST = {
    "system": NonEmpty(str),
    "version": NonEmpty(str),
    "auth": {"kind": NonEmpty(str), "secret_ref": NonEmpty(str)},
    "read_scopes": [str],
    "writes": [{"action_class": NonEmpty(str), "reversible": bool}],
    "provenance": {"author": NonEmpty(str), "job_id": NonEmpty(str), "created_at": NonEmpty(str)},
    "tests": [str],
}


# --- schema ---------------------------------------------------------------------

def schema_errors(artifact: Mapping[str, Any]) -> list[str]:
    found = errors(artifact, {"manifest": MANIFEST, "files": "any", "test_data_ref": NonEmpty(str)})
    manifest = artifact.get("manifest")
    if isinstance(manifest, Mapping) and isinstance(manifest.get("version"), str):
        if not SEMVER.match(manifest["version"]):
            found.append("$.manifest.version: must be semver X.Y.Z")
    files = artifact.get("files")
    if not isinstance(files, Mapping) or not files:
        return found + ["$.files: must be a non-empty object"]
    for name, content in files.items():
        if not isinstance(name, str) or not FILE_NAME.match(name):
            found.append(f"$.files[{name!r}]: flat file name *.py|*.json|*.md expected")
        if not isinstance(content, str):
            found.append(f"$.files[{name!r}]: content must be text")
    if "adapter.py" not in files:
        found.append("$.files: adapter.py is required")
    if sum(len(c) for c in files.values() if isinstance(c, str)) > MAX_FILES_CHARS:
        found.append("$.files: too large")
    return found


def schema(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    found = schema_errors(artifact)
    return CheckOutcome("fail" if found else "pass", {"errors": len(found)}, "\n".join(found))


# --- security (статический разбор) -----------------------------------------------

def _import_allowed(name: str, local: set[str]) -> bool:
    return name in ALLOWED_IMPORTS or name.split(".")[0] in ALLOWED_IMPORTS - {"urllib.parse"} or name in local


def _scan_python(name: str, source: str, local: set[str]) -> list[tuple[str, str]]:
    try:
        tree = ast.parse(source, filename=name)
    except SyntaxError as error:
        return [("syntax", f"{name}:{error.lineno}: {error.msg}")]
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        where = f"{name}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            found += [("import", f"{where}: import {a.name}") for a in node.names if not _import_allowed(a.name, local)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                found.append(("import", f"{where}: relative import"))
            elif not _import_allowed(module, local) and not all(
                _import_allowed(f"{module}.{a.name}", local) for a in node.names
            ):
                found.append(("import", f"{where}: from {module} import ..."))
        elif isinstance(node, ast.Name) and node.id in BANNED_CALLS:
            found.append(("call", f"{where}: {node.id}"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ATTR_FUNCS:
            attr = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str) and not attr.value.startswith("_")):
                found.append(("call", f"{where}: {node.func.id} with dynamic or private name"))
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            found.append(("dunder", f"{where}: .{node.attr}"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id if isinstance(t, ast.Name) else t.attr for t in targets if isinstance(t, (ast.Name, ast.Attribute))]
            value = node.value
            if (
                any(SECRET_NAME.search(n) for n in names)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and len(value.value) >= 8
            ):
                found.append(("secret", f"{where}: string literal assigned to {names}"))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant) and isinstance(key.value, str) and SECRET_NAME.search(key.value)
                    and isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) >= 8
                ):
                    found.append(("secret", f"{where}: string literal under key {key.value!r}"))
    return found


def security_problems(artifact: Mapping[str, Any]) -> list[tuple[str, str]]:
    manifest = artifact.get("manifest") if isinstance(artifact.get("manifest"), Mapping) else {}
    files = artifact.get("files") if isinstance(artifact.get("files"), Mapping) else {}
    found: list[tuple[str, str]] = []

    auth = manifest.get("auth") if isinstance(manifest.get("auth"), Mapping) else {}
    if not SECRET_REF.match(str(auth.get("secret_ref", ""))):
        found.append(("manifest", "auth.secret_ref is not a secret store reference"))
    extra = sorted(set(auth) - {"kind", "secret_ref"})
    if extra:
        found.append(("manifest", f"auth has raw fields {extra}"))
    for scope in manifest.get("read_scopes") or []:
        lowered = str(scope).lower()
        if "*" in lowered or "admin" in lowered or "write" in lowered:
            found.append(("scope", f"read scope too broad: {scope}"))
    found += [("secret", f"manifest: {kind}") for kind in find_secrets(json.dumps(manifest, ensure_ascii=False))]

    local = {Path(n).stem for n in files if isinstance(n, str) and n.endswith(".py")}
    for name, content in files.items():
        if not isinstance(content, str):
            continue
        found += [("secret", f"{name}: {kind}") for kind in find_secrets(content)]
        if str(name).endswith(".py"):
            found += _scan_python(str(name), content, local)
    return found


def security(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    found = security_problems(artifact)
    by_kind: dict[str, int] = {}
    for kind, _ in found:
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return CheckOutcome("fail" if found else "pass", {"problems": by_kind}, "\n".join(m for _, m in found))


# --- sandbox ---------------------------------------------------------------------

def run_in_sandbox(
    files: Mapping[str, str],
    routes: Mapping[str, Any],
    refs: list[str],
    timeout: float = SANDBOX_TIMEOUT,
) -> SandboxRun:
    """Исполнить драфт на тест-стенде без сети и секретов. Статический разбор здесь не делается."""
    return run_adapter(files, routes=routes, refs=refs, timeout=timeout)


_runs: OrderedDict[str, SandboxRun] = OrderedDict()
_runs_lock = threading.Lock()


def _probe_refs(stand: Stand, system: str, manifest: Mapping[str, Any]) -> list[str]:
    refs = list(stand.samples.get(system, ()))
    refs += [ref for ref in manifest.get("tests") or [] if ref not in refs]
    return refs


def sandbox_run(artifact: Mapping[str, Any]) -> SandboxRun:
    """Один прогон на артефакт: sandbox и eval смотрят на один и тот же результат."""
    key = artifact_hash(artifact)
    with _runs_lock:
        if key in _runs:
            return _runs[key]

    if schema_errors(artifact):
        run = SandboxRun(False, "invalid_schema")
    elif security_problems(artifact):
        run = SandboxRun(False, "security_failed")  # небезопасный код не исполняем
    else:
        manifest = artifact["manifest"]
        stand = load_stand(artifact["test_data_ref"])
        system = manifest["system"]
        if stand is None or system not in stand.routes:
            run = SandboxRun(False, "unknown_test_data")
        else:
            run = run_in_sandbox(artifact["files"], stand.routes[system], _probe_refs(stand, system, manifest))

    with _runs_lock:
        _runs[key] = run
        while len(_runs) > 32:
            _runs.popitem(last=False)
    return run


def sandbox(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    run = sandbox_run(artifact)
    metrics = {
        "reason": run.reason or None,
        "blocked": sorted(set(run.blocked)),
        "write_attempts": len(run.writes),
        "duration_ms": run.duration_ms,
    }
    return CheckOutcome("pass" if run.ok else "fail", metrics, run.log)


# --- eval ------------------------------------------------------------------------

def _labels(schema_object: Mapping[str, Any]) -> dict[str, str]:
    return dict(schema_object.get("sensitivity") or {})


def evaluate(artifact: Mapping[str, Any], run: SandboxRun) -> list[str]:
    manifest = artifact["manifest"]
    system = manifest["system"]
    stand = load_stand(artifact["test_data_ref"])
    schema_objects = {o["name"]: o for o in stand.schema(system)["objects"]}
    problems: list[str] = []

    caps = run.result.get("capabilities", {})
    caps_value = caps.get("value") if caps.get("ok") else None
    if not isinstance(caps_value, Mapping) or not isinstance(caps_value.get("reads"), list) or not isinstance(caps_value.get("writes"), list):
        problems.append(f"capabilities() failed or malformed: {caps.get('error')}")
    else:
        declared = sorted(w["action_class"] for w in manifest["writes"])
        actual = sorted(w.get("action_class") if isinstance(w, Mapping) else str(w) for w in caps_value["writes"])
        if declared != actual:
            problems.append(f"capabilities().writes {actual} != manifest.writes {declared}")

    disc = run.result.get("discover", {})
    delta = disc.get("value") if disc.get("ok") else None
    if not isinstance(delta, Mapping) or delta.get("system") != system or not isinstance(delta.get("objects"), list):
        problems.append(f"discover() failed or malformed: {disc.get('error')}")
    else:
        found = {o.get("name"): o for o in delta["objects"] if isinstance(o, Mapping)}
        for name, expected in schema_objects.items():
            if name not in found:
                problems.append(f"discover() misses object {name}")
                continue
            lost = {f: lab for f, lab in _labels(expected).items() if (found[name].get("sensitivity") or {}).get(f) != lab}
            if lost:
                problems.append(f"discover() lost sensitivity for {name}: {sorted(lost)}")

    for ref, outcome in (run.result.get("probes") or {}).items():
        expected = stand.records[system].get(ref)
        facts = outcome.get("value") if outcome.get("ok") else None
        if expected is None:
            problems.append(f"tests[] ref {ref} is not in the test stand")
            continue
        if not isinstance(facts, Mapping):
            problems.append(f"probe({ref}) failed: {outcome.get('error')}")
            continue
        source = facts.get("source") if isinstance(facts.get("source"), Mapping) else {}
        if facts.get("ref") != ref or facts.get("data") != expected:
            problems.append(f"probe({ref}) returned data different from the stand")
        if source.get("system") != system or source.get("object_ref") != ref or not source.get("read_at"):
            problems.append(f"probe({ref}) has no proper source")
        obj = schema_objects.get(ref.split("/", 1)[0], {})
        needed = {f: lab for f, lab in _labels(obj).items() if f in expected}
        got = facts.get("sensitivity") if isinstance(facts.get("sensitivity"), Mapping) else {}
        lost = sorted(f for f, lab in needed.items() if got.get(f) != lab)
        if lost:
            problems.append(f"probe({ref}) lost sensitivity labels {lost}")
    return problems


def eval_(artifact: Mapping[str, Any], context: Context) -> CheckOutcome:
    run = sandbox_run(artifact)
    if not run.ok:
        return CheckOutcome("fail", {"reason": "sandbox_failed"}, run.reason)
    problems = evaluate(artifact, run)
    metrics = {"probes": len(run.result.get("probes") or {}), "problems": len(problems)}
    return CheckOutcome("fail" if problems else "pass", metrics, "\n".join(problems))
