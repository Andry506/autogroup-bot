"""Валидация и нормализация ответов для поля car (марка и модель автомобиля)."""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from app.core.car_data import BRAND_ALIAS_GROUPS, MODEL_ALIAS_GROUPS

_LATIN_RE = re.compile(r"[a-zA-Z]")
_CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")
_DIGIT_RE = re.compile(r"\d")
_MAX_BRAND_WORDS = 3
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

BRAND_ALIASES: dict[str, str] = {}
for canonical, aliases in BRAND_ALIAS_GROUPS:
    for alias in aliases:
        BRAND_ALIASES[alias.lower()] = canonical
    BRAND_ALIASES[canonical.lower()] = canonical


def _normalize_key(text: str) -> str:
    normalized = re.sub(r"[^\w\s\-]", " ", text.lower())
    normalized = normalized.replace("-", " ")
    return re.sub(r"\s+", " ", normalized).strip()


MODEL_ALIASES: dict[str, str] = {}
for canonical, aliases in MODEL_ALIAS_GROUPS:
    for alias in aliases:
        MODEL_ALIASES[_normalize_key(alias)] = canonical
    MODEL_ALIASES[_normalize_key(canonical)] = canonical


MODEL_STOP_WORDS: frozenset[str] = frozenset(
    {
        "цветочек", "цветок", "хороший", "хорошая", "хорошее", "лучший", "лучшая", "лучшее",
        "машина", "машину", "авто", "автомобиль", "какой", "какая", "какое", "какой-то",
        "какая-то", "этот", "эта", "это", "такой", "такая", "такое", "самый", "самая", "самое",
        "красивый", "красивая", "нормальный", "нормальная", "классный", "классная",
        "дорогой", "дорогая", "дешевый", "дешевая", "новый", "новая", "новое", "старый",
        "старая", "большой", "большая", "маленький", "маленькая", "быстрый", "быстрая",
        "мощный", "мощная", "крутой", "крутая", "супер", "класс", "топ", "норм", "ок",
        "да", "нет", "привет", "здравствуйте", "спасибо", "пожалуйста", "хочу", "нужен",
        "нужна", "нужно", "интересует", "интересен", "интересна", "подойдет", "подойдёт",
        "подходит", "вариант", "любой", "любая", "любое", "не знаю", "незнаю", "знаю",
        "пока", "потом", "позже", "хороший", "хорошая", "хорошее",
    }
)

MODEL_COLOR_WORDS: frozenset[str] = frozenset(
    {
        "красный", "красная", "красное", "красную", "красным",
        "черный", "черная", "черное", "чёрный", "чёрная", "чёрное",
        "белый", "белая", "белое",
        "синий", "синяя", "синее",
        "зеленый", "зелёный", "зеленая", "зелёная",
        "серый", "серая", "серое",
        "желтый", "жёлтый", "желтая", "жёлтая",
        "оранжевый", "оранжевая",
        "коричневый", "коричневая",
        "бежевый", "бежевая",
        "фиолетовый", "фиолетовая",
        "розовый", "розовая",
        "red", "black", "white", "blue", "green", "grey", "gray", "yellow", "orange",
    }
)

CAR_REJECT_PHRASES: tuple[str, ...] = (
    "не знаю", "хочу авто", "хочу машину", "пока не знаю",
    "не выбрал", "не определился", "не решил",
    "хороший автомобиль", "красная машина", "красный автомобиль",
)

CAR_REJECT_EXACT: frozenset[str] = frozenset(
    {
        "да", "нет", "yes", "no", "ok", "ок", "ага", "угу", "спасибо", "благодарю",
        "привет", "здравствуйте", "здравствуй", "добрый день", "добрый вечер",
        "доброе утро", "доброй ночи", "хорошо", "норм", "нормально", "готов",
        "продолжим", "понятно", "ясно", "отлично", "супер", "класс", "позже", "потом",
        "не знаю", "хз", "не помню", "зависит", "может быть", "хочу авто",
        "пока не знаю", "не выбрал", "не определился", "не решил", "хочу машину",
        "hello", "hi", "hey",
    }
)

_GREETING_PREFIXES: frozenset[str] = frozenset({"добрый", "доброе", "доброй"})
_GREETING_SUFFIXES: frozenset[str] = frozenset({"день", "утро", "вечер", "ночи"})


class CarParseResult(TypedDict, total=False):
    brand: str
    model: str
    year: str
    generation: str
    confidence: float
    status: str
    source: str


def _tokenize(text: str) -> list[str]:
    normalized = _normalize_key(text)
    if not normalized:
        return []
    return normalized.split()


def resolve_brand(words: list[str]) -> tuple[str | None, list[str]]:
    """Возвращает каноническую марку и оставшиеся слова (модель)."""
    if not words:
        return None, []

    max_len = min(_MAX_BRAND_WORDS, len(words))
    for length in range(max_len, 0, -1):
        phrase = " ".join(words[:length])
        brand = BRAND_ALIASES.get(phrase)
        if brand:
            return brand, words[length:]

    return None, words


def validate_brand(brand: str) -> tuple[str | None, str | None]:
    """Строгая проверка марки через BRAND_ALIASES. Возвращает (канон, status)."""
    if not brand or not brand.strip():
        return None, "unknown_brand"

    normalized = _normalize_key(brand)
    if not normalized:
        return None, "unknown_brand"

    canonical = BRAND_ALIASES.get(normalized)
    if canonical:
        return canonical, None

    words = normalized.split()
    canonical, _ = resolve_brand(words)
    if canonical:
        return canonical, None

    return None, "unknown_brand"


def validate_model_candidate(model: str) -> bool:
    """
    Гибкая проверка модели: не требует наличия в MODEL_ALIASES.
    Отсекает цвета, мусор и общие слова.
    """
    model = (model or "").strip()
    if not model or len(model) < 1:
        return False

    normalized = _normalize_key(model)
    if not normalized:
        return False

    if normalized in MODEL_STOP_WORDS or normalized in MODEL_COLOR_WORDS:
        return False

    if normalized in MODEL_ALIASES:
        return True

    words = normalized.split()
    if all(word in MODEL_STOP_WORDS or word in MODEL_COLOR_WORDS for word in words):
        return False

    if any(word in MODEL_COLOR_WORDS for word in words):
        return False

    has_allowed = False
    for word in words:
        if word in MODEL_STOP_WORDS or word in MODEL_COLOR_WORDS:
            continue
        if word in MODEL_ALIASES:
            has_allowed = True
            continue
        if _DIGIT_RE.search(word):
            has_allowed = True
            continue
        if _LATIN_RE.search(word) and len(word) >= 1:
            has_allowed = True
            continue
        if _CYRILLIC_RE.search(word) and word in MODEL_ALIASES:
            has_allowed = True
            continue
        if _CYRILLIC_RE.search(word):
            return False

    return has_allowed


def _format_latin_model_word(word: str) -> str:
    if _DIGIT_RE.search(word):
        return word.upper()
    lower = word.lower()
    if lower.startswith("i") and len(word) <= 4 and word.isalpha():
        return "i" + word[1:].upper()
    if len(word) <= 3 and word.isalpha():
        return word.upper()
    return word.capitalize()


def _normalize_model_part(model_words: list[str]) -> str:
    if not model_words:
        return ""

    max_len = min(5, len(model_words))
    for length in range(max_len, 0, -1):
        phrase = " ".join(model_words[:length])
        alias = MODEL_ALIASES.get(phrase)
        if alias:
            tail = _normalize_model_part(model_words[length:])
            return f"{alias} {tail}".strip()

    first = model_words[0]
    alias = MODEL_ALIASES.get(first)
    if alias:
        tail = _normalize_model_part(model_words[1:])
        return f"{alias} {tail}".strip()

    formatted = _format_latin_model_word(first)
    if len(model_words) == 1:
        return formatted

    tail = _normalize_model_part(model_words[1:])
    return f"{formatted} {tail}".strip()


def _composite_model_key(brand: str, model_words: list[str]) -> str:
    return _normalize_key(f"{brand} {' '.join(model_words)}")


def _extract_year_generation(words: list[str]) -> tuple[list[str], str, str]:
    year = ""
    generation = ""
    remaining: list[str] = []

    for word in words:
        if not year and _YEAR_RE.fullmatch(word):
            year = word
            continue
        if not generation and re.fullmatch(r"g\d{2}", word, re.IGNORECASE):
            generation = word.upper()
            continue
        remaining.append(word)

    return remaining, year, generation


def _is_valid_model(model_words: list[str], *, brand: str | None = None) -> bool:
    """Legacy fallback: делегирует в validate_model_candidate."""
    if not model_words:
        return False
    if brand:
        composite = _composite_model_key(brand, model_words)
        if composite in MODEL_ALIASES:
            return True
    return validate_model_candidate(" ".join(model_words))


def normalize_car_value(text: str) -> str:
    """Legacy: строковое представление «Brand Model»."""
    parsed = normalize_car(text)
    if parsed.get("status") != "ok":
        return text.strip()
    return format_car_display(parsed)


def normalize_car(
    text: str,
    *,
    brand: str = "",
    model: str = "",
    year: str = "",
    generation: str = "",
) -> CarParseResult:
    """Нормализует марку/модель в структурированный объект."""
    if brand or model:
        canonical_brand, brand_status = validate_brand(brand)
        if brand_status:
            return {"status": brand_status, "brand": "", "model": "", "year": "", "generation": "", "confidence": 0.0}
        model_text = model.strip()
        if not validate_model_candidate(model_text):
            return {"status": "invalid_model", "brand": canonical_brand or "", "model": "", "year": "", "generation": "", "confidence": 0.0}
        model_words = _tokenize(model_text)
        normalized_model = _normalize_model_part(model_words) if model_words else model_text
        return {
            "status": "ok",
            "brand": canonical_brand or "",
            "model": normalized_model,
            "year": year.strip(),
            "generation": generation.strip(),
            "confidence": 0.95,
            "source": "explicit",
        }

    words = _tokenize(text)
    if not words:
        return {"status": "invalid_input", "brand": "", "model": "", "year": "", "generation": "", "confidence": 0.0}

    words, year, generation = _extract_year_generation(words)
    brand_resolved, model_words = resolve_brand(words)
    if not brand_resolved:
        return {"status": "unknown_brand", "brand": "", "model": "", "year": year, "generation": generation, "confidence": 0.0}

    if not model_words:
        return {"status": "needs_model", "brand": brand_resolved, "model": "", "year": year, "generation": generation, "confidence": 0.5}

    composite = _composite_model_key(brand_resolved, model_words)
    alias = MODEL_ALIASES.get(composite)
    if alias:
        brand_norm = _normalize_key(brand_resolved)
        alias_norm = _normalize_key(alias)
        if alias_norm.startswith(f"{brand_norm} "):
            model_value = alias[len(brand_resolved):].strip()
        else:
            model_value = alias
        confidence = 1.0
    else:
        if not validate_model_candidate(" ".join(model_words)):
            return {
                "status": "invalid_model",
                "brand": brand_resolved,
                "model": "",
                "year": year,
                "generation": generation,
                "confidence": 0.0,
            }
        model_value = _normalize_model_part(model_words)
        confidence = 0.85 if any(w in MODEL_ALIASES for w in model_words) else 0.75

    return {
        "status": "ok",
        "brand": brand_resolved,
        "model": model_value,
        "year": year,
        "generation": generation,
        "confidence": confidence,
        "source": "rules",
    }


def parse_car_fast(text: str) -> CarParseResult:
    """Быстрый rule-based разбор без LLM."""
    text_stripped = text.strip()
    if len(text_stripped) < 2:
        return {"status": "invalid_input", "brand": "", "model": "", "year": "", "generation": "", "confidence": 0.0}

    if not (_LATIN_RE.search(text_stripped) or _CYRILLIC_RE.search(text_stripped)):
        return {"status": "invalid_input", "brand": "", "model": "", "year": "", "generation": "", "confidence": 0.0}
    if re.fullmatch(r"[\d\s\W]+", text_stripped):
        return {"status": "invalid_input", "brand": "", "model": "", "year": "", "generation": "", "confidence": 0.0}

    normalized = _normalize_key(text_stripped)
    if normalized in CAR_REJECT_EXACT:
        return {"status": "rejected", "brand": "", "model": "", "year": "", "generation": "", "confidence": 0.0}

    for phrase in CAR_REJECT_PHRASES:
        if phrase in normalized:
            return {"status": "rejected", "brand": "", "model": "", "year": "", "generation": "", "confidence": 0.0}

    words = normalized.split()
    if (
        len(words) >= 2
        and words[0] in _GREETING_PREFIXES
        and words[1] in _GREETING_SUFFIXES
    ):
        return {"status": "rejected", "brand": "", "model": "", "year": "", "generation": "", "confidence": 0.0}

    result = normalize_car(text_stripped)
    if result.get("status") == "ok":
        result["source"] = "rules"
    return result


async def parse_car_with_ai(text: str, llm_client: Any) -> CarParseResult:
    """Извлекает марку/модель через LLM и валидирует результат."""
    extracted, success = await llm_client.extract_car(text)
    if not success:
        return {"status": "ai_failed", "brand": "", "model": "", "year": "", "generation": "", "confidence": 0.0}

    brand_raw = str(extracted.get("brand", "")).strip()
    model_raw = str(extracted.get("model", "")).strip()
    year = str(extracted.get("year", "")).strip()
    generation = str(extracted.get("generation", "")).strip()
    confidence = float(extracted.get("confidence", 0.7) or 0.7)

    canonical_brand, brand_status = validate_brand(brand_raw)
    if brand_status:
        return {"status": "unknown_brand", "brand": "", "model": "", "year": year, "generation": generation, "confidence": 0.0}

    if not model_raw:
        return {"status": "needs_model", "brand": canonical_brand or "", "model": "", "year": year, "generation": generation, "confidence": 0.0}

    if not validate_model_candidate(model_raw):
        return {"status": "invalid_model", "brand": canonical_brand or "", "model": "", "year": year, "generation": generation, "confidence": 0.0}

    normalized = normalize_car(
        text,
        brand=canonical_brand or "",
        model=model_raw,
        year=year,
        generation=generation,
    )
    if normalized.get("status") != "ok":
        return normalized

    normalized["confidence"] = max(confidence, normalized.get("confidence", 0.7))
    normalized["source"] = "ai"
    return normalized


async def parse_car_hybrid(text: str, llm_client: Any) -> CarParseResult:
    """
    Гибридный парсер: сначала правила, при неудаче — LLM.
    Brand = strict, Model = flexible.
    """
    fast = parse_car_fast(text)
    if fast.get("status") == "ok":
        return fast

    if fast.get("status") in {"unknown_brand", "rejected", "invalid_input"}:
        return fast

    ai_result = await parse_car_with_ai(text, llm_client)
    if ai_result.get("status") == "ok":
        return ai_result

    return ai_result if ai_result.get("status") != "ai_failed" else fast


def is_car_answer_valid(text: str) -> bool:
    """Синхронная проверка (быстрый путь + гибкая модель)."""
    result = parse_car_fast(text)
    return result.get("status") == "ok"


def car_to_db(parsed: CarParseResult | dict[str, Any]) -> dict[str, str]:
    """Структура для сохранения в lead.car (JSON)."""
    return {
        "brand": str(parsed.get("brand", "")).strip(),
        "model": str(parsed.get("model", "")).strip(),
        "year": str(parsed.get("year", "")).strip(),
        "generation": str(parsed.get("generation", "")).strip(),
    }


def car_from_storage(value: Any) -> dict[str, str]:
    """Читает lead.car: поддерживает dict, JSON-строку и legacy plain string."""
    if isinstance(value, dict):
        return car_to_db(value)

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return car_to_db({})
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    return car_to_db(data)
            except json.JSONDecodeError:
                pass
        fast = parse_car_fast(raw)
        if fast.get("status") == "ok":
            return car_to_db(fast)
        return {"brand": "", "model": raw, "year": "", "generation": ""}

    return car_to_db({})


def is_car_filled(value: Any) -> bool:
    data = car_from_storage(value)
    return bool(data.get("brand") and data.get("model"))


def format_car_display(value: Any) -> str:
    data = car_from_storage(value)
    brand = data.get("brand", "")
    model = data.get("model", "")
    if not brand and not model:
        return ""

    parts = [brand, model]
    year = data.get("year", "")
    if year:
        parts.append(year)
    generation = data.get("generation", "")
    if generation:
        parts.append(generation)
    return " ".join(part for part in parts if part).strip()
