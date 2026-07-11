import httpx
import json
import logging
import re

from app.core.config import config
from app.core.options import BUDGET_OPTIONS, MARKET_OPTIONS, TIMELINE_OPTIONS

logger = logging.getLogger(__name__)

EMPTY_PARSED = {
    "car": "",
    "budget": "",
    "timeline": "",
    "experience": "",
    "contact": "",
}


def fallback_message() -> str:
    return (
        "Не удалось обработать ваш ответ. Пожалуйста, ответьте на текущий вопрос "
        "или напишите проще, например: «BMW X5»."
    )


def is_empty_parsed(parsed: dict) -> bool:
    return not any(parsed.get(field) for field in EMPTY_PARSED)


class LLMClient:
    """Клиент для работы с OpenRouter API (парсинг сообщений)"""

    def __init__(self):
        self.api_key = config.OPENROUTER_API_KEY
        self.model = config.OPENROUTER_MODEL
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"

    async def parse_message(self, text: str) -> tuple[dict, bool]:
        """
        Парсит сообщение клиента, извлекая поля.

        Returns:
            (parsed_fields, success)
        """
        text = text[: config.MAX_MESSAGE_LENGTH]

        budget_options = ", ".join(f'"{o}"' for o in BUDGET_OPTIONS)
        timeline_options = ", ".join(f'"{o}"' for o in TIMELINE_OPTIONS)
        market_options = ", ".join(f'"{o}"' for o in MARKET_OPTIONS)

        prompt = f"""Извлеки данные из сообщения клиента для автобизнеса.

Поля:
- car: марка и модель автомобиля (не приветствие и не вежливые фразы)
- budget: только один из вариантов: {budget_options}
- timeline: только один из вариантов: {timeline_options}
- experience: рынок покупки — только один из: {market_options}
- contact: телефон или @username в Telegram

Правила:
1. Если поле не найдено, верни пустую строку.
2. Ответь ТОЛЬКО JSON без пояснений.
3. Не добавляй лишних полей.
4. Для budget, timeline и experience используй ТОЛЬКО значения из списков выше.
5. Не извлекай приветствия («добрый день») как car.

Сообщение клиента:
{text}"""

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": "Ты извлекаешь данные из текста. Отвечай только JSON."},
                            {"role": "user", "content": prompt}
                        ],
                        "temperature": 0.1,
                        "max_tokens": 200,
                        "response_format": {"type": "json_object"}
                    }
                )
                response.raise_for_status()

                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = self._parse_json_content(content)
                if is_empty_parsed(parsed):
                    return EMPTY_PARSED.copy(), False
                return parsed, True

        except Exception as e:
            logger.error("Ошибка LLM: %s", e)
            return EMPTY_PARSED.copy(), False

    def _parse_json_content(self, content: str) -> dict:
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if not json_match:
                return EMPTY_PARSED.copy()
            try:
                result = json.loads(json_match.group())
            except json.JSONDecodeError:
                return EMPTY_PARSED.copy()

        normalized = EMPTY_PARSED.copy()
        for field in normalized:
            value = result.get(field, "")
            normalized[field] = str(value).strip() if value else ""
        return normalized
