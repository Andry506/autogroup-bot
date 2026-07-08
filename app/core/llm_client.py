import httpx
import json
import logging
import re

from app.core.config import config

logger = logging.getLogger(__name__)

EMPTY_PARSED = {
    "car": "",
    "budget": "",
    "timeline": "",
    "experience": "",
    "contact": "",
}


class LLMClient:
    """Клиент для работы с OpenRouter API (парсинг сообщений)"""

    def __init__(self):
        self.api_key = config.OPENROUTER_API_KEY
        self.model = config.OPENROUTER_MODEL
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def parse_message(self, text: str) -> dict:
        """
        Парсит сообщение клиента, извлекая поля.
        Используется для первого сообщения или развёрнутых ответов.
        """
        text = text[: config.MAX_MESSAGE_LENGTH]

        prompt = f"""Извлеки данные из сообщения клиента для автобизнеса.

Поля:
- car: марка и модель автомобиля
- budget: бюджет
- timeline: срок покупки
- experience: опыт ввоза авто
- contact: телефон или Telegram

Правила:
1. Если поле не найдено, верни пустую строку.
2. Ответь ТОЛЬКО JSON без пояснений.
3. Не добавляй лишних полей.

Сообщение клиента:
{text}"""

        try:
            response = await self._client.post(
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
            return self._parse_json_content(content)

        except Exception as e:
            logger.error("Ошибка LLM: %s", e)
            return EMPTY_PARSED.copy()

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