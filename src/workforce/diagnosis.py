"""Диагностика сотрудника (ADR-034): общий текст запроса для ручного и авто-вызова.

Диагноз готовит рой самого сотрудника; рекомендация из ответа идёт предложением
куратору и применяется только после согласия (ADR-029, C-006).
"""

from collections.abc import Iterable
from typing import Any


MAX_LINE = 220


def digest(entries: Iterable[Any]) -> str:
    """Компактный дайджест журнала: `- дата kind: ключи payload` (payload уже без секретов)."""
    lines = []
    for entry in entries:
        piece = ", ".join(f"{k}={str(v)[:80]}" for k, v in list(entry.payload.items())[:4])
        lines.append(f"- {entry.created_at:%d.%m %H:%M} {entry.kind}: {piece}"[:MAX_LINE])
    return "\n".join(lines) or "- журнал пуст"


def prompt(question: str, journal_digest: str) -> str:
    return (
        "Режим диагностики. Ты — диагност этого цифрового сотрудника. Ниже журнал его последних событий. "
        "Определи, что сломано или выглядит странно, объясни причину коротко и по делу. "
        "Если видишь, как это исправить — дай рекомендацию (она уйдёт куратору на согласование).\n"
        f"Вопрос куратора: {question or 'разбери последние ошибки и странности'}\n"
        f"Журнал:\n{journal_digest}"
    )
