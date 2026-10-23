"""Реестр систем, которые можно подключить (C-3, «Исследователь»; ADR-023)."""

from dataclasses import dataclass
import json
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from src.landscape.drafter import SYSTEM_ID


@dataclass(frozen=True)
class SystemEntry:
    system: str
    base_url: str
    test_data_ref: str


def parse_registry(raw: str | None) -> dict[str, SystemEntry]:
    """`LANDSCAPE_SYSTEMS`: `{system: {base_url, test_data_ref}}`; пусто — подключать нечего."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, Mapping):
            raise ValueError
        entries = {
            system: SystemEntry(system, entry["base_url"].rstrip("/"), entry["test_data_ref"])
            for system, entry in data.items()
        }
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError("LANDSCAPE_SYSTEMS must be JSON {system: {base_url, test_data_ref}}") from error
    for entry in entries.values():
        parts = urlsplit(entry.base_url)
        if not SYSTEM_ID.match(entry.system) or parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"LANDSCAPE_SYSTEMS has an invalid entry for {entry.system!r}")
        if not re.fullmatch(r"[a-z0-9-]+:[a-z0-9.-]+", entry.test_data_ref):
            raise ValueError(f"LANDSCAPE_SYSTEMS has an invalid test_data_ref for {entry.system!r}")
    return entries
