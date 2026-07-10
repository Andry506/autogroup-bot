import re

CURRENCY_CLARIFICATION_QUESTION = (
    "Уточните, пожалуйста, в какой валюте: USD, EUR или BYN?"
)

CURRENCY_MARKERS = {
    "USD": [r"\busd\b", r"\$", r"доллар", r"dollar"],
    "EUR": [r"\beur\b", r"€", r"евро", r"euro"],
    "BYN": [r"\bbyn\b", r"руб", r"rub", r"белорус"],
}

CURRENCY_STRIP_PATTERN = re.compile(
    r"\b(usd|eur|byn|доллар\w*|евро|рубл\w*|dollars?|euros?|rubles?|белорус\w*)\b|[$€]",
    re.IGNORECASE,
)

THOUSAND_INDICATORS = re.compile(r"тыс|тысяч|\bk\b", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"\d+(?:[\s,]\d+)*")


def detect_currency(text: str) -> str | None:
    text_lower = text.lower()
    for currency, patterns in CURRENCY_MARKERS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                return currency
    return None


def has_currency(text: str) -> bool:
    return detect_currency(text) is not None


def is_pure_number_budget(text: str) -> bool:
    cleaned = re.sub(r"[\s,]", "", text.strip())
    return bool(re.fullmatch(r"\d+", cleaned))


def format_number_with_spaces(digits: str) -> str:
    number = int(digits)
    return f"{number:,}".replace(",", " ")


def expand_shorthand_thousands(text: str) -> str:
    """
    Интерпретирует короткие суммы как тысячи.
    Например: 20 -> 20 000, до 20 -> до 20 000.
    """
    text = text.strip()
    if not text or THOUSAND_INDICATORS.search(text):
        return text

    def replace_number(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits_only = re.sub(r"[\s,]", "", raw)
        if not digits_only.isdigit():
            return raw

        number = int(digits_only)
        if number >= 1000:
            return format_number_with_spaces(digits_only)

        return format_number_with_spaces(str(number * 1000))

    expanded = NUMBER_PATTERN.sub(replace_number, text)
    return re.sub(r"\s+", " ", expanded).strip()


def strip_currency_words(text: str) -> str:
    cleaned = CURRENCY_STRIP_PATTERN.sub("", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def format_budget_with_currency(amount_text: str, currency: str) -> str:
    base = expand_shorthand_thousands(strip_currency_words(amount_text).strip())
    if not base:
        return currency
    if is_pure_number_budget(base):
        digits = re.sub(r"[\s,]", "", base)
        base = format_number_with_spaces(digits)
    return f"{base} {currency}"


def is_currency_only_answer(text: str) -> bool:
    """Ответ состоит только из кода валюты (кнопка USD / EUR / BYN)."""
    cleaned = re.sub(r"[\s\.\,]+", "", text.strip().lower())
    return cleaned in {"usd", "eur", "byn", "$", "€"}


def normalize_budget(text: str) -> tuple[str | None, bool]:
    """
    Нормализует бюджет.

    Returns:
        (normalized_budget, needs_currency_clarification)
    """
    text = text.strip()
    if not text:
        return None, False

    currency = detect_currency(text)
    text = expand_shorthand_thousands(text)
    if not currency:
        currency = detect_currency(text)

    if currency:
        return format_budget_with_currency(text, currency), False

    if re.search(r"\d+", text):
        return None, True

    return text, False
