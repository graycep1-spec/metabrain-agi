"""Шаблонный драфт адаптера для систем с `GET /schema` (C-3, ADR-023)."""

from collections.abc import Mapping
from hashlib import sha256
import json
import re
from typing import Any


SYSTEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

TEMPLATE = '''\
"""Адаптер системы {system} — сгенерирован Исследователем по схеме системы (C-3, ADR-023)."""

from datetime import datetime, timezone

SYSTEM = {system_literal}


class Adapter:
    def __init__(self, http):
        self.http = http
        self.layout = None

    def _layout(self):
        if self.layout is None:
            self.layout = self.http.get("/schema")
        return self.layout

    def _object(self, name):
        for item in self._layout()["objects"]:
            if item["name"] == name:
                return item
        raise LookupError("unknown object " + name)

    def capabilities(self):
        return {{"reads": [item["name"] for item in self._layout()["objects"]], "writes": []}}

    def discover(self):
        objects = []
        for item in self._layout()["objects"]:
            listing = self.http.get(item["path"])
            rows = listing.get("items", [])
            found = dict(item)
            found["count"] = len(rows)
            found["keys"] = [str(row[item["key"]]) for row in rows[:50] if item["key"] in row]
            objects.append(found)
        return {{"system": SYSTEM, "objects": objects}}

    def probe(self, ref):
        name, key = ref.split("/", 1)
        item = self._object(name)
        data = self.http.get(item["path"] + "/" + key)
        labels = {{field: label for field, label in item["sensitivity"].items() if field in data}}
        return {{
            "ref": ref,
            "data": data,
            "source": {{"system": SYSTEM, "object_ref": ref, "read_at": datetime.now(timezone.utc).isoformat()}},
            "sensitivity": labels,
        }}
'''


class DraftRejected(ValueError):
    pass


def check_schema(system: str, schema: Any) -> list[dict[str, Any]]:
    """Схема системы годится для шаблона: объекты с name, key, path, sensitivity."""
    if not isinstance(schema, Mapping) or schema.get("system") != system or not isinstance(schema.get("objects"), list):
        raise DraftRejected("system schema is malformed")
    objects = schema["objects"]
    if not objects:
        raise DraftRejected("system schema has no objects")
    for item in objects:
        if not isinstance(item, Mapping):
            raise DraftRejected("schema object must be an object")
        for field in ("name", "key", "path"):
            if not isinstance(item.get(field), str) or not item[field]:
                raise DraftRejected(f"schema object misses {field}")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", item["name"]) or not re.fullmatch(r"/[a-z0-9_/-]{1,80}", item["path"]):
            raise DraftRejected(f"schema object {item['name']!r} has unsafe name or path")
        if not isinstance(item.get("sensitivity", {}), Mapping):
            raise DraftRejected("schema sensitivity must be an object")
    return [dict(item) for item in objects]


def draft_adapter(
    *, system: str, schema: Any, secret_ref: str, author: str, job_id: str, created_at: str, test_data_ref: str,
) -> dict[str, Any]:
    """Артефакт для профиля Adapter (C-4): манифест, файлы, тест-стенд."""
    if not SYSTEM_ID.match(system):
        raise DraftRejected("invalid system identifier")
    objects = check_schema(system, schema)
    source = TEMPLATE.format(system=system, system_literal=json.dumps(system))
    manifest = {
        "system": system,
        "version": "0.1.0",
        "auth": {"kind": "bearer", "secret_ref": secret_ref},
        "read_scopes": sorted(f"{item['name']}:read" for item in objects),
        "writes": [],
        "provenance": {"author": author, "job_id": job_id, "created_at": created_at},
        "tests": [],
    }
    return {"manifest": manifest, "files": {"adapter.py": source}, "test_data_ref": test_data_ref}


def files_sha256(files: Mapping[str, str]) -> str:
    digest = sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8") + b"\0" + files[name].encode("utf-8") + b"\0")
    return digest.hexdigest()
