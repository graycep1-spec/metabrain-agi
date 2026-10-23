"""Мост адаптера к живой системе: харнесс выполняет GET со своим токеном (C-3, ADR-023)."""

from collections.abc import Callable
import re
from typing import Any

import httpx


SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")
MAX_BODY = 2_000_000


def make_bridge(
    *,
    base_url: str,
    token: str,
    client: httpx.Client,
    on_call: Callable[[str, int], None] | None = None,
) -> Callable[[str, dict[str, str]], tuple[int, Any]]:
    """Только GET, только внутри `base_url`, без `..` и `//`; каждый вызов — в `on_call` (журнал)."""
    root = base_url.rstrip("/")

    def bridge(path: str, params: dict[str, str]) -> tuple[int, Any]:
        if not SAFE_PATH.match(path) or ".." in path or "//" in path:
            status = 400
        else:
            response = client.get(f"{root}{path}", params=params, headers={"Authorization": f"Bearer {token}"},
                                  follow_redirects=False)
            status = response.status_code
            if status == 200:
                if len(response.content) > MAX_BODY:
                    status = 413
                else:
                    if on_call:
                        on_call(path, status)
                    return 200, response.json()
        if on_call:
            on_call(path, status)
        return status, None

    return bridge
