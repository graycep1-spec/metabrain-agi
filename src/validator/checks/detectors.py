"""Детерминированные детекторы чувствительных данных для pii_dlp и security (C-4)."""

import re


SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "openai_like_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "telegram_bot_token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer_literal": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
}

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(
    r"(?<![\d\w])(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"
    r"|(?<![\d\w])\+\d{1,3}[\s(-]*\d{2,4}[\s)-]*\d{3}[\s-]*\d{2,4}(?:[\s-]*\d{2,4})?(?!\d)"
)
CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
PASSPORT_RU = re.compile(r"паспорт\D{0,20}(\d{2}\s?\d{2}\s?\d{6})(?!\d)", re.IGNORECASE)
SNILS = re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[\s-]\d{2}(?!\d)")

# Категории, которые не допустимы ни в одном исходящем тексте.
ALWAYS_FORBIDDEN = ("card", "passport_ru", "snils", "secret")
# Контакты клиента допустимы в ответе владельцу харнесса, но не в навыке компании.
CONTACTS = ("email", "phone")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def find_secrets(text: str) -> list[str]:
    return [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text)]


def detect(text: str) -> dict[str, int]:
    """Число находок по категориям: card, passport_ru, snils, secret, email, phone."""
    found = {
        "card": sum(
            1
            for match in CARD_CANDIDATE.finditer(text)
            if 13 <= len(digits := re.sub(r"\D", "", match.group())) <= 19 and _luhn(digits)
        ),
        "passport_ru": len(PASSPORT_RU.findall(text)),
        "snils": len(SNILS.findall(text)),
        "secret": len(find_secrets(text)),
        "email": len(EMAIL.findall(text)),
        "phone": len(PHONE.findall(text)),
    }
    return {name: count for name, count in found.items() if count}
