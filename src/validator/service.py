"""Сервис validate(artifact, profile, context) -> ScoreCard (C-4)."""

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from src.validator.scorecard import (
    PROFILES,
    CheckResult,
    Context,
    ScoreCard,
    ScoreCardSigner,
    Verdict,
    artifact_hash,
)


# Состав профилей — по таблице C-4. Порядок чекеров попадает в ScoreCard.
# В Adapter security стоит до sandbox: небезопасный код не исполняется.
PROFILE_CHECKS: dict[str, tuple[str, ...]] = {
    "answer": ("grounding", "pii_dlp", "judge"),
    "passport": ("schema", "rights", "autonomy"),
    "adapter": ("schema", "security", "sandbox", "eval"),
    "action": ("policy_scope", "capability", "limits", "preview", "consent"),
    "skill": ("schema", "pii_dlp", "foreign_data", "eval", "provenance"),
}
assert set(PROFILE_CHECKS) == set(PROFILES)

# Наборы профилей (ADR-016): demo-v1 — без LLM-судьи и eval навыка.
PROFILE_SETS: dict[str, dict[str, tuple[str, ...]]] = {
    "mvp-v1": PROFILE_CHECKS,
    "demo-v1": {
        **PROFILE_CHECKS,
        "answer": ("grounding", "pii_dlp"),
        "skill": ("schema", "pii_dlp", "foreign_data", "provenance"),
    },
}
DEFAULT_PROFILE_SET = "mvp-v1"

NOT_IMPLEMENTED = "not_implemented"


class InvalidRequest(ValueError):
    pass


class SigningUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckOutcome:
    """Контракт чекера: `{verdict, metrics, log}` (C-4)."""

    verdict: Verdict
    metrics: dict[str, Any] = field(default_factory=dict)
    log: str = ""


class Checker(Protocol):
    def __call__(self, artifact: Mapping[str, Any], context: Context) -> CheckOutcome: ...


class Validator:
    """Детерминированный гейт: pass, только если прошли все чекеры профиля.

    Чекер, которого нет в реестре или который упал с исключением, даёт fail:
    гейт закрыт по умолчанию (C-005).
    """

    def __init__(
        self,
        signer: ScoreCardSigner | None,
        checkers: Mapping[tuple[str, str], Checker] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        log_capacity: int = 1000,
        profile_set: str = DEFAULT_PROFILE_SET,
    ):
        if profile_set not in PROFILE_SETS:
            raise ValueError(f"unknown profile set: {profile_set!r}")
        self.profile_set = profile_set
        self._profiles = PROFILE_SETS[profile_set]
        self._signer = signer
        self._checkers = dict(checkers or {})
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._logs: OrderedDict[str, str] = OrderedDict()
        self._log_capacity = log_capacity

    @property
    def signer(self) -> ScoreCardSigner | None:
        return self._signer

    def validate(
        self, artifact: Any, profile: Any, context: Any
    ) -> ScoreCard:
        if self._signer is None:
            raise SigningUnavailable("validator signing key is not configured")
        if profile not in self._profiles:
            raise InvalidRequest(f"unknown profile: {profile!r}")
        if not isinstance(artifact, Mapping):
            raise InvalidRequest("artifact must be an object")
        try:
            parsed_context = Context.from_dict(context)
        except ValueError as error:
            raise InvalidRequest(str(error)) from error

        scorecard_id = uuid4()
        checks = tuple(
            self._run(scorecard_id, profile, name, artifact, parsed_context)
            for name in self._profiles[profile]
        )
        verdict: Verdict = "pass" if all(c.verdict == "pass" for c in checks) else "fail"
        unsigned = ScoreCard(
            scorecard_id=scorecard_id,
            artifact_hash=artifact_hash(artifact),
            profile=profile,
            profile_set=self.profile_set,
            context=parsed_context,
            verdict=verdict,
            checks=checks,
            issued_at=self._clock(),
            key_id=self._signer.key_id,
        )
        return self._signer.sign(unsigned)

    def log(self, log_ref: str) -> str | None:
        """Лог чекера по `log_ref`; только внутри сервиса, наружу не отдаётся."""
        return self._logs.get(log_ref)

    def _run(
        self,
        scorecard_id,
        profile: str,
        name: str,
        artifact: Mapping[str, Any],
        context: Context,
    ) -> CheckResult:
        checker = self._checkers.get((profile, name))
        if checker is None:
            outcome = CheckOutcome("fail", {"reason": NOT_IMPLEMENTED})
        else:
            try:
                outcome = checker(artifact, context)
                if outcome.verdict not in ("pass", "fail"):
                    raise ValueError(f"invalid verdict {outcome.verdict!r}")
            except Exception as error:  # чекер упал — гейт закрыт
                outcome = CheckOutcome(
                    "fail",
                    {"reason": "checker_error"},
                    f"{type(error).__name__}: {error}",
                )
        log_ref = f"{scorecard_id}/{name}"
        self._remember(log_ref, outcome.log)
        return CheckResult(name, outcome.verdict, dict(outcome.metrics), log_ref)

    def _remember(self, log_ref: str, log: str) -> None:
        self._logs[log_ref] = log
        while len(self._logs) > self._log_capacity:
            self._logs.popitem(last=False)
