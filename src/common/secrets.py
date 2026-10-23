import os
import re
from collections.abc import Mapping


class SecretNotFoundError(RuntimeError):
    pass


class EnvSecretStore:
    _reference_pattern = re.compile(r"env:([A-Z][A-Z0-9_]*)\Z")

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = os.environ if environ is None else environ

    def resolve(self, secret_ref: str) -> str:
        if not isinstance(secret_ref, str):
            raise SecretNotFoundError("secret reference must use the env:NAME format")
        match = self._reference_pattern.fullmatch(secret_ref)
        if match is None:
            raise SecretNotFoundError("secret reference must use the env:NAME format")

        name = match.group(1)
        value = self._environ.get(name)
        if value is None or not value.strip():
            raise SecretNotFoundError(f"required secret is not configured: env:{name}")
        return value
