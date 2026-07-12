"""Валидация контактных данных."""

import re


def is_valid_contact(text: str) -> bool:
    """Проверяет телефон (7–15 цифр) или @username."""
    text_stripped = text.strip()
    if re.search(r"@\w+", text_stripped):
        return True
    without_username = re.sub(r"@\w+", "", text_stripped)
    if re.search(r"[a-zA-Zа-яА-ЯёЁ]", without_username):
        return False
    digits = re.sub(r"\D", "", text_stripped)
    return 7 <= len(digits) <= 15
