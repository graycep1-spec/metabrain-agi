"""Реестр чекеров по (профиль, имя) (C-4, правила — раздел «Чекеры»).

Чекера, которого нет в реестре, валидатор считает fail с причиной `not_implemented`:
сейчас это `judge` (Answer), `eval` (Skill) и весь профиль Action — их нет в наборе
`demo-v1` (ADR-016) и они появятся в полном объёме T-12.
"""

from collections.abc import Mapping

from src.validator.checks import adapter, answer, passport, skill
from src.validator.service import Checker


def default_checkers(grounding_threshold: float = 0.0) -> Mapping[tuple[str, str], Checker]:
    return {
        ("answer", "grounding"): answer.grounding(grounding_threshold),
        ("answer", "pii_dlp"): answer.pii_dlp,
        ("passport", "schema"): passport.schema,
        ("passport", "rights"): passport.rights,
        ("passport", "autonomy"): passport.autonomy,
        ("adapter", "schema"): adapter.schema,
        ("adapter", "security"): adapter.security,
        ("adapter", "sandbox"): adapter.sandbox,
        ("adapter", "eval"): adapter.eval_,
        ("skill", "schema"): skill.schema,
        ("skill", "pii_dlp"): skill.pii_dlp,
        ("skill", "foreign_data"): skill.foreign_data,
        ("skill", "provenance"): skill.provenance,
    }
