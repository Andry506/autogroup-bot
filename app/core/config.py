import os
import base64
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)

load_dotenv()


def _decode_credentials_json(raw: str) -> str:
    """Декодирует GOOGLE_CREDENTIALS_JSON из base64 в JSON-строку."""
    if not raw:
        return ""
    raw = raw.strip()
    try:
        decoded_bytes = base64.b64decode(raw, validate=True)
    except Exception as e:
        logger.error("GOOGLE_CREDENTIALS_JSON: ошибка base64-декодирования: %s", e)
        return ""
    for encoding in ("utf-8", "cp1251"):
        try:
            return decoded_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    logger.error(
        "GOOGLE_CREDENTIALS_JSON: данные после base64 не являются UTF-8 или CP1251. "
        "Перекодируйте credentials.json в UTF-8 перед base64."
    )
    return ""


class Config:
    # === TELEGRAM ===
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "")
    MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID")
    
    # === OPENROUTER ===
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    
    # === GOOGLE SHEETS ===
    GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
    GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    _credentials_json_raw = os.getenv("GOOGLE_CREDENTIALS_JSON", "")


    GOOGLE_CREDENTIALS_JSON = _decode_credentials_json(_credentials_json_raw)

    # === НАСТРОЙКИ ===
    DEBUG = os.getenv("DEBUG", "True").lower() == "true"
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = int(os.getenv("PORT", "8000"))
    
    # === БЕЗОПАСНОСТЬ ===
    RATE_LIMIT_MESSAGES = int(os.getenv("RATE_LIMIT_MESSAGES", "10"))
    RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
    MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "1000"))
    MAX_DIALOG_ENTRIES = int(os.getenv("MAX_DIALOG_ENTRIES", "20"))
    
    # === НАПОМИНАНИЯ ===
    REMINDER_DELAY_SECONDS = int(os.getenv("REMINDER_DELAY_SECONDS", "300"))
    REMINDER_INTERVAL_SECONDS = int(os.getenv("REMINDER_INTERVAL_SECONDS", "300"))
    REMINDER_MAX_COUNT = int(os.getenv("REMINDER_MAX_COUNT", "2"))

def validate_config():
    """Проверяет, что все обязательные переменные заданы"""
    required = ["BOT_TOKEN", "OPENROUTER_API_KEY", "WEBHOOK_URL"]
    missing = [var for var in required if not getattr(config, var)]
    
    if missing:
        raise ValueError(f"❌ Отсутствуют обязательные переменные: {', '.join(missing)}")
    
    if config.MANAGER_CHAT_ID:
        try:
            int(config.MANAGER_CHAT_ID)
        except ValueError:
            raise ValueError("❌ MANAGER_CHAT_ID должен быть числом")
    
    logger.info("✅ Конфигурация валидна")
    return True

config = Config()