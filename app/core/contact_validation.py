"""Валидация и форматирование контактных данных."""

import re

INVALID_COUNTRY_CODE_MESSAGE = (
    "Пожалуйста, проверьте код страны и введите номер заново."
)

SUPPORTED_COUNTRY_CODES: tuple[str, ...] = (
    "+971", "+966", "+995", "+994", "+998", "+993", "+992", "+996",
    "+374", "+373", "+372", "+371", "+370", "+358", "+357", "+356",
    "+375", "+380", "+86", "+82", "+81", "+66", "+65", "+60",
    "+62", "+84", "+91", "+55", "+54", "+52", "+61", "+64", "+27",
    "+49", "+48", "+44", "+39", "+34", "+33", "+31", "+32", "+41",
    "+46", "+47", "+45", "+30", "+90", "+7", "+1",
)

_COUNTRY_CODES_SORTED = tuple(sorted(SUPPORTED_COUNTRY_CODES, key=len, reverse=True))


def count_phone_digits(text: str) -> int:
    return len(re.sub(r"\D", "", text))


def is_username_contact(text: str) -> bool:
    return bool(re.search(r"@\w+", text.strip()))


def _digits_only(text: str) -> str:
    return re.sub(r"\D", "", text)


def _match_country_code(digits: str) -> tuple[str | None, str]:
    for code in _COUNTRY_CODES_SORTED:
        code_digits = code[1:]
        if digits.startswith(code_digits):
            return code, digits[len(code_digits):]
    return None, digits


def _infer_country_code(digits: str) -> tuple[str | None, str]:
    if digits.startswith("375") and len(digits) >= 10:
        return "+375", digits[3:]
    if digits.startswith("380") and len(digits) >= 11:
        return "+380", digits[3:]
    if digits.startswith("80") and len(digits) >= 10:
        return "+375", digits[2:]
    if digits.startswith("8") and len(digits) == 11:
        return "+7", digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return "+7", digits[1:]
    if digits.startswith("1") and len(digits) == 11:
        return "+1", digits[1:]
    return _match_country_code(digits)


def _format_national_number(country_code: str, national: str) -> str:
    national = national.lstrip("0")
    if country_code == "+375" and len(national) >= 9:
        return f"({national[:2]}) {national[2:5]}-{national[5:7]}-{national[7:9]}"
    if country_code == "+7" and len(national) >= 10:
        return f"({national[:3]}) {national[3:6]}-{national[6:8]}-{national[8:10]}"
    if country_code == "+380" and len(national) >= 9:
        return f"({national[:2]}) {national[2:5]}-{national[5:7]}-{national[7:9]}"
    if country_code == "+1" and len(national) >= 10:
        return f"({national[:3]}) {national[3:6]}-{national[6:10]}"
    if country_code == "+44" and len(national) >= 10:
        return f"{national[:4]} {national[4:7]} {national[7:]}"
    if country_code == "+49" and len(national) >= 10:
        return f"{national[:3]} {national[3:6]} {national[6:]}"
    if country_code == "+48" and len(national) >= 9:
        return f"{national[:3]} {national[3:6]} {national[6:9]}"
    if len(national) >= 9:
        return f"{national[:3]} {national[3:6]}-{national[6:8]}-{national[8:]}"
    if len(national) >= 7:
        return f"{national[:3]} {national[3:]}"
    return national


def format_phone_number(text: str) -> str:
    stripped = text.strip()
    digits = _digits_only(stripped)

    if stripped.startswith("+"):
        country_code, national = _match_country_code(digits)
    else:
        country_code, national = _infer_country_code(digits)

    if not country_code:
        raise ValueError(INVALID_COUNTRY_CODE_MESSAGE)

    formatted_national = _format_national_number(country_code, national)
    return f"{country_code} {formatted_national}".strip()


def validate_phone_number(text: str) -> tuple[bool, str]:
    stripped = text.strip()
    if not re.fullmatch(r"[\d\s\(\)\-\+]+", stripped):
        return False, INVALID_COUNTRY_CODE_MESSAGE

    digits = _digits_only(stripped)
    if not 7 <= len(digits) <= 15:
        return False, (
            "Пожалуйста, введите корректный номер телефона "
            "(например, +375291234567)."
        )

    if stripped.startswith("+"):
        country_code, _ = _match_country_code(digits)
        if not country_code:
            return False, INVALID_COUNTRY_CODE_MESSAGE
    else:
        country_code, _ = _infer_country_code(digits)
        if not country_code:
            return False, INVALID_COUNTRY_CODE_MESSAGE

    return True, ""


def is_phone_number_valid(text: str) -> bool:
    if is_username_contact(text):
        return True
    ok, _ = validate_phone_number(text)
    return ok


def is_valid_contact(text: str) -> bool:
    """Проверяет телефон (7–15 цифр, поддерживаемый код страны) или @username."""
    return is_phone_number_valid(text)


def normalize_contact(text: str) -> tuple[bool, str, str]:
    stripped = text.strip()
    if is_username_contact(stripped):
        return True, stripped, ""

    ok, error = validate_phone_number(stripped)
    if not ok:
        return False, "", error

    try:
        return True, format_phone_number(stripped), ""
    except ValueError as exc:
        return False, "", str(exc)
