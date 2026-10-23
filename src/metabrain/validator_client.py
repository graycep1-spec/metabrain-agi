"""Клиент валидатора перенесён в src.validator.client; модуль оставлен для совместимости."""

from src.validator.client import HttpValidatorClient, ValidatorClient, ValidatorUnavailable

__all__ = ["HttpValidatorClient", "ValidatorClient", "ValidatorUnavailable"]
