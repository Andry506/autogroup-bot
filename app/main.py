import asyncio
import logging
import os
import re
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from fastapi import FastAPI, Request, HTTPException
from sqlalchemy.orm import sessionmaker
from app.core.car_validation import (
    car_to_db,
    format_car_display,
    is_car_answer_valid,
    is_car_filled,
    parse_car_fast,
    parse_car_hybrid,
)
from app.core.config import config, validate_config
from app.core.database import engine, Base, ensure_schema
from app.core.llm_client import LLMClient, fallback_message, is_empty_parsed
from app.core.options import BUDGET_OPTIONS, MARKET_OPTIONS, TIMELINE_OPTIONS
from app.core.telegram_utils import format_client_summary, format_manager_notification
from app.integrations.google_sheets import GoogleSheetsClient
from app.models.lead import Lead
from app.services.fsm_service import FSMService, LEAD_FIELDS, LeadField
from app.services.rate_limiter import RateLimiter
from app.services.reminder_service import (
    POSTPONE_ACK_MESSAGE,
    ReminderService,
    is_later_response,
)

# === НАСТРОЙКА ЛОГОВ ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# === ВАЛИДАЦИЯ КОНФИГА ===
validate_config()

# === СОЗДАНИЕ ТАБЛИЦ ===
logger.info("🔄 Создание таблиц в базе данных...")
Base.metadata.create_all(bind=engine)
ensure_schema()
logger.info("✅ Таблицы созданы")

# === FASTAPI ===
app = FastAPI(
    title="AI Auto Agency",
    description="Бот для сбора заявок в автобизнесе (Polling Mode)",
    version="1.0.0",
)

# === TELEGRAM БОТ ===
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# === СЕРВИСЫ ===
llm = LLMClient()
sheets_client = GoogleSheetsClient()
rate_limiter = RateLimiter(
    max_requests=config.RATE_LIMIT_MESSAGES,
    window_seconds=config.RATE_LIMIT_WINDOW,
)
reminder_service = ReminderService(bot)

# === БАЗА ДАННЫХ ===
Session = sessionmaker(bind=engine)

PENDING_FIELD_CONFIRM_KEY = "pending_field_confirm"
WAITING_FOR_USER_KEY = "waiting_for_user"
REMINDER_ACTIVE_KEY = "reminder_active"
REMINDER_QUESTION_KEY = "reminder_question"
REMINDER_DELAY_KEY = "reminder_delay_seconds"
REMINDER_MAX_KEY = "reminder_max_reminders"
REMINDER_INTERVAL_KEY = "reminder_interval_seconds"
REMINDER_STARTED_AT_KEY = "reminder_started_at"
AWAITING_MANUAL_CONTACT_KEY = "awaiting_manual_contact"


CHANGE_KEYWORDS = [
    "передумал",
    "хочу",
    "давай",
    "изменим",
    "поменяем",
    "вместо",
    "не тот",
]

FIELD_LABELS = {
    "car": "авто",
    "budget": "бюджет",
    "timeline": "срок",
    "experience": "рынок",
    "contact": "контакт",
}

VALIDATED_FIELDS = ["car", "budget", "timeline", "experience", "contact"]
BUTTON_FIELDS = frozenset({"budget", "timeline", "experience"})

YES_CONFIRM_WORDS = ["да", "yes", "ага", "верно", "подтверждаю", "ок", "ok"]
NO_CONFIRM_WORDS = ["нет", "no", "неа", "оставь", "отмена", "не надо"]


COMPLETED_LEAD_HINT = (
    "✅ Ваша заявка уже принята. Менеджер свяжется с вами в ближайшее время.\n\n"
    "Чтобы оставить новую заявку, отправьте /new"
)

HELP_TEXT = (
    "ℹ️ <b>Справка — AutoGroup Bot</b>\n\n"
    "Я AI-помощник компании AutoGroup. Помогаю собрать заявку на автомобиль "
    "«мечты» и передать данные менеджеру.\n\n"
    "<b>Как пользоваться:</b>\n"
    "• Отвечайте на вопросы по очереди\n"
    "• Для бюджета, срока и рынка используйте кнопки\n"
    "• Напишите «позже», если нужно время на ответ\n\n"
    "<b>Команды:</b>\n"
    "/start — начать работу с ботом\n"
    "/new — оставить новую заявку\n"
    "/cancel — отменить текущий диалог\n"
    "/help — показать эту справку"
)


def get_welcome_text() -> str:
    return (
        "Здравствуйте! Я AI-помощник компании AutoGroup!\n"
        "Я помогу собрать заявку на автомобиль \"мечты\"!\n"
        "Пожалуйста, отвечайте на вопросы, и я передам данные менеджеру."
    )

def get_welcome_with_car_question() -> str:
    car_question = FSMService.get_question_for_field(LeadField.CAR)
    return f"{get_welcome_text()}\n\n{car_question}"


def get_active_lead(db, chat_id: str) -> Lead | None:
    """Возвращает незавершённую заявку пользователя."""
    return (
        db.query(Lead)
        .filter(Lead.chat_id == chat_id, Lead.status != "completed")
        .order_by(Lead.created_at.desc())
        .first()
    )


def get_pending_state(db, chat_id: str) -> dict:
    """Возвращает pending_state активного лида из БД."""
    lead = get_active_lead(db, chat_id)
    if not lead or not lead.pending_state:
        return {}
    return dict(lead.pending_state)


def set_pending_state(db, chat_id: str, state: dict | None) -> None:
    """Сохраняет pending_state активного лида в БД."""
    lead = get_active_lead(db, chat_id)
    if not lead:
        return
    lead.pending_state = state or {}


def is_awaiting_manual_contact(db, chat_id: str) -> bool:
    return bool(get_pending_state(db, chat_id).get(AWAITING_MANUAL_CONTACT_KEY))


def set_awaiting_manual_contact(db, chat_id: str, value: bool = True) -> None:
    state = get_pending_state(db, chat_id)
    if value:
        state[AWAITING_MANUAL_CONTACT_KEY] = True
    else:
        state.pop(AWAITING_MANUAL_CONTACT_KEY, None)
    set_pending_state(db, chat_id, state)


def is_waiting_for_user(db, chat_id: str) -> bool:
    return bool(get_pending_state(db, chat_id).get(WAITING_FOR_USER_KEY))


def set_waiting_for_user(db, chat_id: str, value: bool = True) -> None:
    state = get_pending_state(db, chat_id)
    if value:
        state[WAITING_FOR_USER_KEY] = True
    else:
        state.pop(WAITING_FOR_USER_KEY, None)
    set_pending_state(db, chat_id, state)


def clear_waiting_for_user(db, chat_id: str) -> None:
    set_waiting_for_user(db, chat_id, False)


async def handle_postpone_request(message: types.Message, chat_id: str) -> None:
    """Обрабатывает отложенный ответ: отменяет напоминание, сохраняет FSM."""
    db = Session()
    try:
        reminder_service.acknowledge_postpone(chat_id)
        lead = get_active_lead(db, chat_id)
        if lead:
            clear_reminder_state(db, chat_id)
            set_waiting_for_user(db, chat_id, True)
            dialog = list(lead.dialog_history or [])
            dialog.append(
                {
                    "role": "user",
                    "text": message.text.strip() if message.text else "позже",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            dialog.append(
                {
                    "role": "assistant",
                    "text": POSTPONE_ACK_MESSAGE,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            lead.dialog_history = trim_dialog_history(dialog)
            db.commit()
            logger.info("⏸️ Пользователь отложил ответ: chat_id=%s, lead_id=%s", chat_id, lead.id)
        else:
            logger.info("⏸️ Пользователь отложил ответ без активной заявки: chat_id=%s", chat_id)

        await send_reply(message, POSTPONE_ACK_MESSAGE)
    except Exception as e:
        logger.error("❌ Ошибка обработки отложенного ответа: %s", e, exc_info=True)
        await send_reply(message, POSTPONE_ACK_MESSAGE)
        db.rollback()
    finally:
        db.close()


def persist_reminder_state(
    db,
    chat_id: str,
    question_text: str,
    delay_seconds: int,
    max_reminders: int,
    interval_seconds: int,
) -> None:
    state = get_pending_state(db, chat_id)
    state.update(
        {
            REMINDER_ACTIVE_KEY: True,
            REMINDER_QUESTION_KEY: question_text,
            REMINDER_DELAY_KEY: delay_seconds,
            REMINDER_MAX_KEY: max_reminders,
            REMINDER_INTERVAL_KEY: interval_seconds,
            REMINDER_STARTED_AT_KEY: datetime.now(timezone.utc).isoformat(),
        }
    )
    set_pending_state(db, chat_id, state)


def clear_reminder_state(db, chat_id: str) -> None:
    state = get_pending_state(db, chat_id)
    for key in (
        REMINDER_ACTIVE_KEY,
        REMINDER_QUESTION_KEY,
        REMINDER_DELAY_KEY,
        REMINDER_MAX_KEY,
        REMINDER_INTERVAL_KEY,
        REMINDER_STARTED_AT_KEY,
    ):
        state.pop(key, None)
    set_pending_state(db, chat_id, state)


def cancel_reminder_for_chat(db, chat_id: str) -> None:
    reminder_service.cancel_reminder(chat_id)
    clear_reminder_state(db, chat_id)


def build_lead_row(lead: Lead) -> dict:
    return {
        "chat_id": lead.chat_id,
        "username": lead.username,
        "car": format_car_display(lead.car),
        "budget": lead.budget,
        "timeline": lead.timeline,
        "experience": lead.experience,
        "contact": lead.contact,
        "status": lead.status,
    }


def export_lead_to_sheets(lead: Lead) -> bool:
    return sheets_client.add_lead(build_lead_row(lead))


def retry_failed_exports(db) -> int:
    """Повторяет экспорт заявок со статусом failed. Возвращает число успешных."""
    failed_leads = db.query(Lead).filter(Lead.export_status == "failed").all()
    if not failed_leads:
        return 0

    recovered = 0
    for lead in failed_leads:
        try:
            if export_lead_to_sheets(lead):
                lead.export_status = "exported"
                recovered += 1
                logger.info("♻️ Повторный экспорт успешен: lead_id=%s", lead.id)
        except Exception as e:
            logger.error("❌ Повторный экспорт не удался для lead_id=%s: %s", lead.id, e)

    if recovered:
        db.commit()
    return recovered


async def restore_reminders_from_db() -> None:
    """Восстанавливает активные напоминания после перезапуска."""
    db = Session()
    try:
        leads = db.query(Lead).filter(Lead.status != "completed").all()
        restored = 0
        dirty = False

        for lead in leads:
            state = lead.pending_state or {}
            if state.get(WAITING_FOR_USER_KEY):
                continue
            if not state.get(REMINDER_ACTIVE_KEY):
                continue

            question = state.get(REMINDER_QUESTION_KEY, "")
            if not question:
                clear_reminder_state(db, lead.chat_id)
                dirty = True
                continue

            delay_seconds = int(state.get(REMINDER_DELAY_KEY, config.REMINDER_DELAY_SECONDS))
            max_reminders = int(state.get(REMINDER_MAX_KEY, config.REMINDER_MAX_COUNT))
            interval_seconds = int(
                state.get(REMINDER_INTERVAL_KEY, config.REMINDER_INTERVAL_SECONDS)
            )

            started_at_raw = state.get(REMINDER_STARTED_AT_KEY)
            if started_at_raw:
                started_at = datetime.fromisoformat(started_at_raw)
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                delay_seconds = max(0, int(delay_seconds - elapsed))

            reminder_service.schedule_reminder(
                chat_id=lead.chat_id,
                question_text=question,
                delay_seconds=delay_seconds,
                max_reminders=max_reminders,
                interval_seconds=interval_seconds,
            )
            restored += 1

        if dirty or restored:
            db.commit()
            logger.info("♻️ Восстановлено напоминаний: %s", restored)
    finally:
        db.close()


# === КЛАВИАТУРЫ (БЕЗ ЭМОДЗИ В ДАННЫХ) ===
def get_budget_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="до 20 000 USD")],
            [KeyboardButton(text="20 000 - 40 000 USD")],
            [KeyboardButton(text="40 000 - 60 000 USD")],
            [KeyboardButton(text="более 60 000 USD")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_timeline_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1-3 месяца")],
            [KeyboardButton(text="3-6 месяцев")],
            [KeyboardButton(text="более 6 месяцев")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_market_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="США"), KeyboardButton(text="Европа")],
            [KeyboardButton(text="Корея"), KeyboardButton(text="Китай")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_experience_keyboard() -> ReplyKeyboardMarkup:
    """Алиас для совместимости — клавиатура выбора рынка."""
    return get_market_keyboard()


def get_new_application_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/new")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def get_contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)],
            [KeyboardButton(text="Ввести вручную")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()

# === КАКУЮ КЛАВИАТУРУ ПОКАЗАТЬ ДЛЯ ПОЛЯ ===
def get_keyboard_for_field(field_name: str):
    keyboards = {
        "budget": get_budget_keyboard(),
        "timeline": get_timeline_keyboard(),
        "experience": get_experience_keyboard(),
        "contact": get_contact_keyboard(),
    }
    return keyboards.get(field_name)

# === ПРОВЕРКА ОТВЕТА (ПОНЯТНЫЙ / НЕ ПОНЯТНЫЙ) ===
def normalize_car_text(text: str) -> str:
    normalized = re.sub(r"[^\w\s\-]", " ", text.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def is_known_car_brand(word: str) -> bool:
    return word.lower().strip() in CAR_KNOWN_BRANDS


def is_car_answer_valid(text: str) -> bool:
    """
    Проверяет, похож ли ответ на марку/модель автомобиля.
    Допускается: «BMW X5» (2+ слова) или одно слово — известная марка («BMW»).
    Отклоняются: приветствия, вежливые слова, «да», «ок» и т.п.
    """
    text_stripped = text.strip()
    if len(text_stripped) < 2:
        return False

    if not re.search(r"[a-zA-Zа-яА-ЯёЁ]", text_stripped):
        return False
    if re.fullmatch(r"[\d\s\W]+", text_stripped):
        return False

    normalized = normalize_car_text(text_stripped)
    if not normalized:
        return False

    if normalized in CAR_REJECT_EXACT:
        return False

    for phrase in CAR_REJECT_PHRASES:
        if phrase in normalized:
            return False

    words = normalized.split()
    if all(word in CAR_REJECT_WORDS for word in words):
        return False

    if len(words) >= 2 and words[0] in {"добрый", "доброе", "доброй"}:
        if words[1] in {"день", "утро", "вечер", "ночи"}:
            return False

    if len(words) == 1:
        return is_known_car_brand(words[0])

    if len(words) >= 2:
        if words[0] in CAR_REJECT_WORDS:
            return False
        return True

    return False


def is_answer_valid(text: str, field_name: str) -> bool:
    """
    Проверяет, является ли ответ понятным для поля.
    Если ответ невнятный — вернет False, и бот покажет кнопки.
    """
    text_stripped = text.strip()

    if field_name == "car":
        return is_car_answer_valid(text_stripped)

    if field_name == "budget":
        return text_stripped in BUDGET_OPTIONS

    if field_name == "timeline":
        return text_stripped in TIMELINE_OPTIONS

    if field_name == "experience":
        return text_stripped in MARKET_OPTIONS

    if field_name == "contact":
        if re.search(r"@\w+", text_stripped):
            return True
        cleaned_text = re.sub(r"[\s\(\)\-]", "", text_stripped)
        if re.search(r"\+?\d{10,15}", cleaned_text):
            return not re.search(r"[a-zA-Zа-яА-ЯёЁ]", re.sub(r"@\w+", "", text_stripped))
        return False

    return True


def get_invalid_answer_message(field_name: str) -> str:
    messages = {
        "car": (
            "Пожалуйста, напишите марку и модель автомобиля "
            "(например, BMW X5, Тойота Камри или BYD Song)."
        ),
        "budget": "Пожалуйста, выберите вариант бюджета из кнопок ниже:",
        "timeline": "Пожалуйста, выберите срок из кнопок ниже:",
        "experience": "Пожалуйста, выберите рынок из кнопок ниже:",
        "contact": (
            "Пожалуйста, укажите корректный контакт: номер телефона "
            "или @username в Telegram."
        ),
    }
    return messages.get(field_name, f"🤔 Не совсем понял. Пожалуйста, уточните ответ для поля '{field_name}'.")

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_lead_data(lead: Lead) -> dict[str, str]:
    data: dict[str, str] = {}
    for field in LEAD_FIELDS:
        if field == "car":
            data[field] = format_car_display(lead.car) if is_car_filled(lead.car) else ""
        else:
            value = getattr(lead, field) or ""
            data[field] = value.strip() if isinstance(value, str) else str(value)
    return data

def trim_dialog_history(dialog: list) -> list:
    if len(dialog) > config.MAX_DIALOG_ENTRIES:
        return dialog[-config.MAX_DIALOG_ENTRIES :]
    return dialog

def should_use_llm(
    lead_data: dict[str, str],
    text: str,
    expected_field: LeadField | None = None,
) -> bool:
    text_lower = text.lower()

    if expected_field and expected_field.value == "car":

        return False

    if expected_field and is_answer_valid(text, expected_field.value):
        return False

    if has_change_keywords(text_lower):
        return True

    if message_touches_multiple_fields(text_lower):
        return True

    # Всегда вызываем LLM для длинных сообщений
    if len(text) > 50 or "\n" in text or text.count(",") >= 1:
        return True

    # Если есть хотя бы одно поле — проверяем, нужно ли обновление
    filled_count = sum(1 for field in LEAD_FIELDS if lead_data.get(field))
    if filled_count == 0:
        return True

    return False


def has_change_keywords(text_lower: str) -> bool:
    return any(keyword in text_lower for keyword in CHANGE_KEYWORDS)


def message_touches_multiple_fields(text_lower: str) -> bool:
    """Эвристика: сообщение похоже на ответ сразу по нескольким полям."""
    topic_count = 0

    car_markers = [
        "bmw",
        "audi",
        "toyota",
        "mercedes",
        "lexus",
        "машин",
        "авто",
        "модел",
        "хочу",
    ]
    if any(marker in text_lower for marker in car_markers) or re.search(
        r"[a-z]{2,}\s+[a-z0-9\-]+", text_lower
    ):
        topic_count += 1

    budget_markers = ["бюджет", "usd", "eur", "byn", "доллар", "евро", "тыс", "$", "€"]
    if any(marker in text_lower for marker in budget_markers) or re.search(
        r"\d[\d\s]{2,}", text_lower
    ):
        topic_count += 1

    timeline_markers = ["месяц", "недел", "срочн", "скоро", "срок", "когда"]
    if any(marker in text_lower for marker in timeline_markers):
        topic_count += 1

    market_markers = ["сша", "европ", "коре", "китай", "рынок", "рынке"]
    if any(marker in text_lower for marker in market_markers):
        topic_count += 1

    contact_markers = ["телефон", "номер", "@", "контакт"]
    if any(marker in text_lower for marker in contact_markers) or re.search(
        r"\+?\d{10,15}", text_lower
    ):
        topic_count += 1

    return topic_count >= 2


def is_contact_like_text(text: str) -> bool:
    """Определяет, похож ли текст на номер телефона или @username."""
    if text.strip() in BUDGET_OPTIONS:
        return False
    if re.search(r"@\w+", text):
        return True
    cleaned_text = re.sub(r"[\s\(\)\-]", "", text)
    return bool(re.search(r"\+?\d{10,15}", cleaned_text))


def log_fsm_state(
    lead: Lead,
    chat_id: str,
    stage: str,
    *,
    expected_field: LeadField | None = None,
    next_field: LeadField | None = None,
) -> dict[str, str]:
    """Логирует текущее состояние FSM и возвращает lead_data."""
    lead_data = get_lead_data(lead)
    logger.info(
        "🔄 FSM [%s] chat_id=%s lead_id=%s expected=%s next=%s data=%s",
        stage,
        chat_id,
        lead.id,
        expected_field.value if expected_field else None,
        next_field.value if next_field else None,
        lead_data,
    )
    return lead_data


def apply_expected_field_answer(
    lead: Lead,
    db,
    chat_id: str,
    expected_field: LeadField,
    text: str,
) -> bool:
    """Сохраняет ответ пользователя для текущего поля FSM."""
    field_name = expected_field.value
    if not is_answer_valid(text, field_name):
        return False

    if field_name in {"budget", "timeline", "experience"}:
        value = text.strip()
    elif field_name == "contact":
        value = clean_text(text)
    else:
        value = clean_text(text)
    if field_name == "budget":
        lead.budget = value
    elif field_name == "contact":
        lead.contact = value
        set_awaiting_manual_contact(db, chat_id, False)
    else:
        setattr(lead, field_name, value)

    logger.info("📝 Поле %s заполнено для lead_id=%s: %s", field_name, lead.id, value)
    return True


def get_reply_markup_for_field(db, chat_id: str, field: LeadField):
    if field == LeadField.CONTACT and not is_awaiting_manual_contact(db, chat_id):
        return get_contact_keyboard()
    if field == LeadField.BUDGET:
        return get_budget_keyboard()
    if field == LeadField.TIMELINE:
        return get_timeline_keyboard()
    if field == LeadField.EXPERIENCE:
        return get_market_keyboard()
    return None


def get_pending_field_confirm(db, chat_id: str) -> dict | None:
    confirm = get_pending_state(db, chat_id).get(PENDING_FIELD_CONFIRM_KEY)
    return dict(confirm) if confirm else None


def set_pending_field_confirm(db, chat_id: str, confirm: dict | None) -> None:
    state = get_pending_state(db, chat_id)
    if confirm:
        state[PENDING_FIELD_CONFIRM_KEY] = confirm
    else:
        state.pop(PENDING_FIELD_CONFIRM_KEY, None)
    set_pending_state(db, chat_id, state)


def is_yes_confirmation(text: str) -> bool:
    text_lower = text.lower().strip()
    return any(word == text_lower or text_lower.startswith(f"{word} ") for word in YES_CONFIRM_WORDS)


def is_no_confirmation(text: str) -> bool:
    text_lower = text.lower().strip()
    return any(word == text_lower or text_lower.startswith(f"{word} ") for word in NO_CONFIRM_WORDS)


async def apply_parsed_fields(
    lead: Lead,
    parsed: dict[str, str],
    *,
    allow_overwrite: bool = False,
) -> dict | None:
    """
    Безопасно применяет поля из LLM.
    Возвращает данные для подтверждения, если нужно изменить заполненное поле.
    """
    for field in LEAD_FIELDS:
        value = parsed.get(field, "")
        if not value:
            continue

        clean_value = clean_text(value)
        if not clean_value:
            continue
        if field == "car":
            car_result = await parse_car_hybrid(clean_value, llm)
            if car_result.get("status") != "ok":
                continue
            clean_value = car_to_db(car_result)
        if field == "budget" and clean_value not in BUDGET_OPTIONS:
            continue
        if field == "timeline" and clean_value not in TIMELINE_OPTIONS:
            continue
        if field == "experience" and clean_value not in MARKET_OPTIONS:
            continue
        if field == "contact" and not is_answer_valid(clean_value, "contact"):
            continue

        current_raw = getattr(lead, field)
        if field == "car":
            current_value = format_car_display(current_raw) if is_car_filled(current_raw) else ""
            new_value = format_car_display(clean_value)
        else:
            current_value = (current_raw or "").strip() if isinstance(current_raw, str) else str(current_raw or "")
            new_value = clean_value if isinstance(clean_value, str) else str(clean_value)
        if current_value and current_value != new_value and not allow_overwrite:
            label = FIELD_LABELS.get(field, field)
            return {
                "field": field,
                "old_value": current_value,
                "new_value": clean_value,
                "question": (
                    f"Вы хотите изменить {label} с «{current_value}» на «{new_value}»?\n"
                    "Ответьте «да» или «нет»."
                ),
            }

        setattr(lead, field, clean_value)

    return None


async def send_current_field_prompt(
    message: types.Message,
    db,
    chat_id: str,
    expected_field,
) -> None:
    if not expected_field:
        await send_reply(
            message,
            "Сначала, пожалуйста, ответьте на текущий вопрос.",
        )
        return

    question = FSMService.get_question_for_field(expected_field)
    reply_markup = get_reply_markup_for_field(db, chat_id, expected_field)
    await send_reply(
        message,
        f"Сначала, пожалуйста, ответьте на текущий вопрос:\n\n{question}",
        reply_markup=reply_markup,
    )


async def send_llm_fallback_with_question(
    message: types.Message,
    db,
    chat_id: str,
    expected_field,
) -> None:
    await send_reply(message, fallback_message())
    await send_current_field_prompt(message, db, chat_id, expected_field)

def clean_text(text: str) -> str:
    """Очищает текст от эмодзи и лишних символов"""
    if not text:
        return ""
    # Удаляем эмодзи
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # смайлики
        "\U0001F300-\U0001F5FF"  # символы и пиктограммы
        "\U0001F680-\U0001F6FF"  # транспорт и карты
        "\U0001F700-\U0001F77F"  # алхимические символы
        "\U0001F780-\U0001F7FF"  # геометрические фигуры
        "\U0001F800-\U0001F8FF"  # стрелки
        "\U0001F900-\U0001F9FF"  # дополнительные символы
        "\U0001FA00-\U0001FA6F"  # дополнительные символы
        "\U0001FA70-\U0001FAFF"  # дополнительные символы
        "\U00002702-\U000027B0"  # декоративные символы
        "\U000024C2-\U0001F251"  # дополнительные символы
        "]+",
        flags=re.UNICODE
    )
    return emoji_pattern.sub('', text).strip()


def apply_budget_value(lead: Lead, chat_id: str, raw_budget: str, db) -> None:
    """Сохраняет бюджет только из предустановленных вариантов кнопок."""
    raw_budget = clean_text(raw_budget).strip()
    if raw_budget in BUDGET_OPTIONS:
        lead.budget = raw_budget


async def send_reply(message: types.Message, text: str, **kwargs) -> None:
    """Отправляет ответ через bot.send_message (надёжно работает в webhook-режиме)."""
    chat_id = message.chat.id
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        logger.info("📤 Ответ отправлен в chat_id=%s", chat_id)
    except Exception as e:
        logger.error("❌ Ошибка отправки: %s", e, exc_info=True)
        raise


async def schedule_question_reminder(
    db,
    chat_id: str,
    question: str,
) -> None:
    reminder_service.schedule_reminder(
        chat_id=chat_id,
        question_text=question,
        delay_seconds=config.REMINDER_DELAY_SECONDS,
        max_reminders=config.REMINDER_MAX_COUNT,
        interval_seconds=config.REMINDER_INTERVAL_SECONDS,
    )
    persist_reminder_state(
        db,
        chat_id,
        question,
        config.REMINDER_DELAY_SECONDS,
        config.REMINDER_MAX_COUNT,
        config.REMINDER_INTERVAL_SECONDS,
    )


async def begin_lead_dialog(
    message: types.Message,
    db,
    lead: Lead,
    *,
    include_welcome: bool = True,
) -> None:
    """Отправляет стартовое сообщение и планирует напоминание для первого вопроса."""
    chat_id = str(message.chat.id)
    welcome_text = get_welcome_with_car_question() if include_welcome else FSMService.get_question_for_field(LeadField.CAR)

    await send_reply(message, welcome_text)
    await schedule_question_reminder(db, chat_id, welcome_text)

    dialog = list(lead.dialog_history or [])
    dialog.append(
        {
            "role": "assistant",
            "text": welcome_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    lead.dialog_history = trim_dialog_history(dialog)
    db.commit()
    logger.info("🆕 Начат диалог для lead_id=%s", lead.id)

# === ЗАВЕРШЕНИЕ ЗАЯВКИ ===
async def finalize_lead(lead: Lead, message: types.Message, db, dialog: list) -> None:
    if lead.status == "completed":
        return

    chat_id = str(message.chat.id)
    cancel_reminder_for_chat(db, chat_id)

    lead.status = "completed"
    lead.export_status = "pending"
    lead.dialog_history = dialog

    # Убираем клавиатуру
    await send_reply(
        message,
        "✅ Диалог завершен",
        reply_markup=remove_keyboard(),
    )

    # Отправляем клиенту
    await send_reply(
        message,
        format_client_summary(lead),
        parse_mode=ParseMode.HTML,
    )

    await send_reply(
        message,
        "Хотите оставить ещё одну заявку? Нажмите кнопку ниже или отправьте /new",
        reply_markup=get_new_application_keyboard(),
    )

    # Уведомление в группу или менеджеру
    notification_chat_id = config.GROUP_CHAT_ID or config.MANAGER_CHAT_ID
    if notification_chat_id:
        try:
            await bot.send_message(
                chat_id=int(notification_chat_id),
                text=format_manager_notification(lead),
                parse_mode=ParseMode.HTML,
            )
            if config.GROUP_CHAT_ID:
                logger.info("📨 Уведомление отправлено в группу: lead_id=%s", lead.id)
            else:
                logger.info("📨 Уведомление отправлено менеджеру: lead_id=%s", lead.id)
        except Exception as e:
            if config.GROUP_CHAT_ID:
                logger.error("❌ Ошибка отправки уведомления в группу: %s", e)
            else:
                logger.error("❌ Ошибка отправки уведомления менеджеру: %s", e)

    # Сохраняем в Google Sheets
    try:
        saved = await asyncio.to_thread(export_lead_to_sheets, lead)
        lead.export_status = "exported" if saved else "failed"
        if saved:
            logger.info("📊 Заявка сохранена в Google Sheets: lead_id=%s", lead.id)
        else:
            logger.error("❌ Google Sheets недоступен, export_status=failed: lead_id=%s", lead.id)
    except Exception as e:
        lead.export_status = "failed"
        logger.error("❌ Ошибка сохранения в Google Sheets: %s", e)

    db.commit()
    logger.info(
        "✅ Заявка завершена: lead_id=%s, export_status=%s",
        lead.id,
        lead.export_status,
    )

# === КОМАНДЫ БОТА ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    chat_id = str(message.chat.id)
    username = message.from_user.username or "unknown"
    db = Session()
    try:
        has_completed = (
            db.query(Lead)
            .filter(Lead.chat_id == chat_id, Lead.status == "completed")
            .count()
            > 0
        )
        if has_completed:
            await send_reply(message, COMPLETED_LEAD_HINT)
            return

        active_lead = get_active_lead(db, chat_id)
        if active_lead:
            await send_reply(
                message,
                "У вас уже есть активная заявка. Продолжайте диалог или отправьте /cancel.",
            )
            return

        cancel_reminder_for_chat(db, chat_id)
        lead = Lead(chat_id=chat_id, username=username, pending_state={})
        db.add(lead)
        db.flush()
        await begin_lead_dialog(message, db, lead)
        logger.info("👋 /start: создан лид lead_id=%s для chat_id=%s", lead.id, chat_id)
    except Exception as e:
        logger.error("❌ Ошибка команды /start: %s", e, exc_info=True)
        await send_reply(message, "⚠️ Не удалось начать работу с ботом. Попробуйте позже.")
        db.rollback()
    finally:
        db.close()


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await send_reply(message, HELP_TEXT, parse_mode=ParseMode.HTML)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    chat_id = str(message.chat.id)
    db = Session()
    try:
        cancel_reminder_for_chat(db, chat_id)
        deleted = (
            db.query(Lead)
            .filter(Lead.chat_id == chat_id, Lead.status != "completed")
            .delete()
        )
        db.commit()
        await send_reply(
            message,
            "Диалог отменён. Чтобы начать заново, отправьте /start",
            reply_markup=remove_keyboard(),
        )
        logger.info("🛑 Диалог отменён для chat_id=%s, удалено заявок: %s", chat_id, deleted)
    except Exception as e:
        logger.error("❌ Ошибка отмены диалога: %s", e)
        await send_reply(message, "⚠️ Ошибка при отмене диалога. Попробуйте позже.")
        db.rollback()
    finally:
        db.close()


@dp.message(Command("new"))
async def cmd_new(message: types.Message):
    chat_id = str(message.chat.id)
    username = message.from_user.username or "unknown"
    db = Session()
    try:
        active_lead = get_active_lead(db, chat_id)
        if active_lead:
            await send_reply(
                message,
                "У вас уже есть активная заявка. Продолжайте диалог или отправьте /cancel.",
            )
            return

        cancel_reminder_for_chat(db, chat_id)
        lead = Lead(chat_id=chat_id, username=username, pending_state={})
        db.add(lead)
        db.flush()
        await begin_lead_dialog(message, db, lead)
    except Exception as e:
        logger.error("❌ Ошибка команды /new: %s", e, exc_info=True)
        await send_reply(message, "⚠️ Не удалось начать новую заявку. Попробуйте позже.")
        db.rollback()
    finally:
        db.close()


# === ТЕСТОВАЯ КОМАНДА ДЛЯ ОЧИСТКИ ===
@dp.message(lambda message: message.text and message.text.startswith('/clean'))
async def clean_my_leads(message: types.Message):
    """Удаляет все заявки текущего пользователя (только для тестов)"""
    chat_id = str(message.chat.id)
    
    # === ЗАЩИТА: ТОЛЬКО ДЛЯ ТВОЕГО ID ===
    YOUR_CHAT_ID = "971853859"
    if chat_id != YOUR_CHAT_ID:
        await send_reply(message, "⛔ У вас нет прав на эту команду.")
        return
    # =====================================
    
    db = Session()
    try:
        deleted = db.query(Lead).filter(Lead.chat_id == chat_id).delete()
        db.commit()
        
        if deleted > 0:
            await send_reply(message, f"✅ Удалено {deleted} тестовых заявок. Можно начинать новый диалог!")
        else:
            await send_reply(message, "ℹ️ У вас нет активных заявок для удаления.")
            
        logger.info(f"🧹 Очистка: удалено {deleted} заявок для chat_id={chat_id}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка очистки: {e}")
        await send_reply(message, "⚠️ Ошибка при очистке. Попробуйте позже.")
        db.rollback()
    finally:
        db.close()

# === ОБРАБОТЧИК СООБЩЕНИЙ ===
@dp.message()
async def handle_message(message: types.Message):
    # Игнорируем сообщения из групп (чтобы бот не отвечал)
    if message.chat.type in ("group", "supergroup"):
        return

    chat_id = str(message.chat.id)
    username = message.from_user.username or "unknown"

    # === РЕЙТ-ЛИМИТ ===
    if not rate_limiter.is_allowed(chat_id):
        await send_reply(
            message,
            "⚠️ Слишком много сообщений. Подождите минуту и попробуйте снова.",
        )
        return

    # === ОТЛОЖИТЬ ОТВЕТ («позже», «потом» и т.д.) ===
    if message.text and is_later_response(message.text.strip()):
        await handle_postpone_request(message, chat_id)
        return

    # === ОБРАБОТКА КНОПКИ "ВВЕСТИ ВРУЧНУЮ" ===
    if message.text and message.text == "Ввести вручную":
        db = Session()
        try:
            lead = get_active_lead(db, str(message.chat.id))
            if lead:
                set_awaiting_manual_contact(db, str(message.chat.id), True)
                db.commit()
            await send_reply(
                message,
                "✍️ Введите номер телефона в формате:\n"
                "• +375 29 123 45 67\n"
                "• 8 (029) 123-45-67\n"
                "• или @username",
                reply_markup=remove_keyboard(),
            )
        finally:
            db.close()
        return

    # === НОВАЯ ЗАЯВКА ЧЕРЕЗ КНОПКУ ===
    if message.text and message.text.strip() in {"/new", "Оставить новую заявку"}:
        await cmd_new(message)
        return

    # === ПОДГОТОВКА ТЕКСТА / КОНТАКТА ===
    shared_contact = message.contact

    if shared_contact:
        text = clean_text(shared_contact.phone_number)
    elif message.text:
        text = message.text.strip()
        if not text:
            await send_reply(message, "Пожалуйста, отправьте текстовое сообщение.")
            return

        if len(text) > config.MAX_MESSAGE_LENGTH:
            await send_reply(
                message,
                f"⚠️ Сообщение слишком длинное. Максимум {config.MAX_MESSAGE_LENGTH} символов.",
            )
            return
    else:
        await send_reply(message, "Пожалуйста, отправьте текстовое сообщение.")
        return

    # === ЛОГИРОВАНИЕ ===
    logger.info("📩 Сообщение от chat_id=%s, длина=%s", chat_id, len(text))

    db = Session()

    try:
        was_waiting_for_user = False
        lead = get_active_lead(db, chat_id)
        if lead and is_waiting_for_user(db, chat_id):
            clear_waiting_for_user(db, chat_id)
            was_waiting_for_user = True
            db.commit()
            logger.info("▶️ Пользователь вернулся к диалогу: chat_id=%s", chat_id)

        cancel_reminder_for_chat(db, chat_id)
        db.commit()

        lead = get_active_lead(db, chat_id)
        is_new_lead = False
        just_sent_welcome = False

        if not lead:
            has_completed = (
                db.query(Lead)
                .filter(Lead.chat_id == chat_id, Lead.status == "completed")
                .count()
                > 0
            )
            if has_completed:
                await send_reply(message, COMPLETED_LEAD_HINT)
                return

            lead = Lead(chat_id=chat_id, username=username, pending_state={})
            db.add(lead)
            db.flush()
            is_new_lead = True
            logger.info("🆕 Создан новый лид: lead_id=%s", lead.id)
        elif username != "unknown" and lead.username != username:
            lead.username = username

        if is_new_lead:
            await begin_lead_dialog(message, db, lead)
            just_sent_welcome = True

        # 3. ПОЛУЧАЕМ ТЕКУЩИЕ ДАННЫЕ
        lead_data = get_lead_data(lead)
        expected_field = FSMService.get_next_field(lead_data)
        log_fsm_state(lead, chat_id, "expected_field", expected_field=expected_field)

        # После «позже» — переспрос текущего вопроса при невалидном ответе
        if (
            was_waiting_for_user
            and expected_field
            and not is_answer_valid(text, expected_field.value)
        ):
            dialog = list(lead.dialog_history or [])
            dialog.append(
                {
                    "role": "user",
                    "text": text,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            lead.dialog_history = trim_dialog_history(dialog)
            await send_current_field_prompt(message, db, chat_id, expected_field)
            db.commit()
            return

        # 3.0 ПОДТВЕРЖДЕНИЕ ИЗМЕНЕНИЯ ПОЛЯ
        skip_field_processing = False
        pending_confirm = get_pending_field_confirm(db, chat_id)
        if pending_confirm:
            if is_yes_confirmation(text):
                field_name = pending_confirm["field"]
                setattr(lead, field_name, pending_confirm["new_value"])
                if field_name == "contact":
                    set_awaiting_manual_contact(db, chat_id, False)
                set_pending_field_confirm(db, chat_id, None)
                skip_field_processing = True
                db.commit()
                logger.info(
                    "✅ Поле %s обновлено после подтверждения для lead_id=%s",
                    field_name,
                    lead.id,
                )
            elif is_no_confirmation(text):
                set_pending_field_confirm(db, chat_id, None)
                dialog = list(lead.dialog_history or [])
                dialog.append(
                    {
                        "role": "user",
                        "text": text,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                lead.dialog_history = trim_dialog_history(dialog)
                await send_reply(message, "Хорошо, оставляю прежнее значение.")
                await send_current_field_prompt(message, db, chat_id, expected_field)
                db.commit()
                return
            else:
                dialog = list(lead.dialog_history or [])
                dialog.append(
                    {
                        "role": "user",
                        "text": text,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                lead.dialog_history = trim_dialog_history(dialog)
                await send_reply(message, pending_confirm["question"])
                db.commit()
                return

        # 3.05 КОНТАКТ ТОЛЬКО НА ШАГЕ CONTACT
        if shared_contact is not None:
            if not expected_field or expected_field != LeadField.CONTACT:
                await send_current_field_prompt(message, db, chat_id, expected_field)
                return
            lead.contact = clean_text(shared_contact.phone_number)
            set_awaiting_manual_contact(db, chat_id, False)
            skip_field_processing = True
            logger.info("📱 Получен номер телефона: %s", lead.contact)
        elif (
            expected_field
            and expected_field != LeadField.CONTACT
            and is_contact_like_text(text)
            and not is_answer_valid(text, expected_field.value)
        ):
            await send_current_field_prompt(message, db, chat_id, expected_field)
            return

        if not skip_field_processing:

            # 4. ЕСЛИ ЕСТЬ ОЖИДАЕМОЕ ПОЛЕ — ПРОВЕРЯЕМ ОТВЕТ
            car_parse_result = None
            if (
                not skip_field_processing
                and expected_field
                and expected_field.value in VALIDATED_FIELDS
            ):
                field_name = expected_field.value
                if field_name == "car":
                    car_parse_result = await parse_car_hybrid(text, llm)
                    is_valid = car_parse_result.get("status") == "ok"
                else:
                    is_valid = is_answer_valid(text, field_name)

                if not is_valid:
                    keyboard = None if (
                        field_name == "contact" and is_awaiting_manual_contact(db, chat_id)
                    ) else get_keyboard_for_field(field_name)
                    message_text = get_invalid_answer_message(field_name)
                    if keyboard:
                        await send_reply(
                            message,
                            message_text,
                            reply_markup=keyboard,
                        )
                        logger.info(
                            "❓ Показаны варианты для поля %s, chat_id=%s",
                            field_name,
                            chat_id,
                        )
                        return
                    await send_reply(message, message_text)
                    return

            # 5. СОХРАНЕНИЕ ОТВЕТА НА ТЕКУЩИЙ ВОПРОС (без LLM)
            field_answer_applied = False
            if (
                not skip_field_processing
                and expected_field
                and expected_field.value in VALIDATED_FIELDS
            ):
                if expected_field.value == "car" and car_parse_result:
                    if car_parse_result.get("status") == "ok":
                        lead.car = car_to_db(car_parse_result)
                        field_answer_applied = True
                        logger.info(
                            "📝 Поле car заполнено для lead_id=%s: %s",
                            lead.id,
                            format_car_display(lead.car),
                        )
                else:
                    field_answer_applied = apply_expected_field_answer(
                        lead, db, chat_id, expected_field, text
                    )
                if field_answer_applied and expected_field.value in BUTTON_FIELDS:
                    skip_field_processing = True
                    db.commit()
                    lead_data = log_fsm_state(
                        lead,
                        chat_id,
                        f"saved_{expected_field.value}",
                        expected_field=expected_field,
                        next_field=FSMService.get_next_field(get_lead_data(lead)),
                    )

            # 6. ПАРСИНГ (если нужно)
            # Проверяем, не является ли сообщение ТОЛЬКО приветствием
            greeting_words = ["привет", "добрый день", "добрый вечер", "здравствуйте", "здравствуй"]
            # Очищаем текст от пунктуации и лишних пробелов
            cleaned_text = re.sub(r'[^\w\s]', '', text).strip().lower()
            words = cleaned_text.split()

            # Если в сообщении ТОЛЬКО приветствие (1-2 слова) и нет других данных
            is_pure_greeting = (
                len(words) <= 3 and
                any(word in cleaned_text for word in greeting_words)
            )

            # Если это чистое приветствие и у лида нет данных — не парсим
            if is_pure_greeting and not any(lead_data.values()):
                logger.info(f"👋 Обнаружено чистое приветствие, пропускаем парсинг для lead_id={lead.id}")
                if just_sent_welcome:
                    return
            elif not field_answer_applied and not was_waiting_for_user and should_use_llm(lead_data, text, expected_field):
                parsed, llm_success = await llm.parse_message(text)
                if not llm_success or is_empty_parsed(parsed):
                    await send_llm_fallback_with_question(message, db, chat_id, expected_field)
                    return

                if parsed.get("budget"):
                    apply_budget_value(lead, chat_id, parsed["budget"], db)
                    parsed["budget"] = ""

                confirm_request = await apply_parsed_fields(
                    lead,
                    parsed,
                    allow_overwrite=has_change_keywords(text.lower()),
                )
                if confirm_request:
                    set_pending_field_confirm(db, chat_id, confirm_request)
                    dialog = list(lead.dialog_history or [])
                    dialog.append(
                        {
                            "role": "user",
                            "text": text,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    lead.dialog_history = trim_dialog_history(dialog)
                    await send_reply(message, confirm_request["question"])
                    db.commit()
                    return

                logger.info("🧠 LLM-парсинг выполнен для lead_id=%s", lead.id)
                lead_data = get_lead_data(lead)
                logger.info("📋 Данные после LLM: %s", lead_data)

        # 7. СОХРАНЕНИЕ ИСТОРИИ
        dialog = list(lead.dialog_history or [])
        dialog.append(
            {
                "role": "user",
                "text": text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        lead.dialog_history = trim_dialog_history(dialog)

        # 8. ОПРЕДЕЛЕНИЕ СЛЕДУЮЩЕГО ВОПРОСА
        lead_data = get_lead_data(lead)
        next_field = FSMService.get_next_field(lead_data)
        log_fsm_state(
            lead,
            chat_id,
            "next_field",
            expected_field=expected_field,
            next_field=next_field,
        )

        if next_field:
            if just_sent_welcome and next_field == LeadField.CAR:
                return

            question = FSMService.get_question_for_field(next_field)
            reply_markup = get_reply_markup_for_field(db, chat_id, next_field)
            await send_reply(message, question, reply_markup=reply_markup)

            await schedule_question_reminder(db, chat_id, question)
            logger.info("⏰ Напоминание запланировано для chat_id=%s", chat_id)

            dialog.append(
                {
                    "role": "assistant",
                    "text": question,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            lead.dialog_history = trim_dialog_history(dialog)
            db.commit()
            logger.info("❓ Задан вопрос для поля %s, lead_id=%s", next_field.value, lead.id)
        else:
            await finalize_lead(lead, message, db, dialog)

    except Exception as e:
        logger.error("❌ Ошибка обработки сообщения: %s", e, exc_info=True)
        try:
            await send_reply(message, "⚠️ Произошла ошибка. Пожалуйста, попробуйте ещё раз.")
        except Exception:
            pass
        db.rollback()
    finally:
        db.close()

# === ЭНДПОИНТЫ FASTAPI ===
@app.get("/")
async def root():
    return {
        "status": "running",
        "mode": "polling",
        "message": "AI Auto Agency is alive!",
        "version": "1.0.0",
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.on_event("startup")
async def on_startup():
    reminder_service.start()
    
    await restore_reminders_from_db()

    db = Session()
    try:
        recovered = retry_failed_exports(db)
        if recovered:
            logger.info("♻️ Повторно экспортировано заявок: %s", recovered)
    finally:
        db.close()

    if not config.WEBHOOK_SECRET_TOKEN:
        logger.warning(
            "⚠️ WEBHOOK_SECRET_TOKEN не задан. Эндпоинт /webhook/telegram принимает запросы без проверки."
        )

    try:
        await bot.set_webhook(
            url=config.WEBHOOK_URL,
            secret_token=config.WEBHOOK_SECRET_TOKEN or None,
            drop_pending_updates=True,
        )
        logger.info("🔗 Webhook установлен: %s", config.WEBHOOK_URL)
    except Exception as e:
        logger.error("❌ Ошибка установки webhook: %s", e, exc_info=True)
    else:
        try:
            webhook_info = await bot.get_webhook_info()
            if webhook_info.url == config.WEBHOOK_URL:
                logger.info("✅ Webhook успешно установлен и проверен")
            else:
                logger.warning(
                    "⚠️ Webhook не соответствует ожидаемому URL: %s (ожидался %s)",
                    webhook_info.url,
                    config.WEBHOOK_URL,
                )
        except Exception as e:
            logger.error("❌ Ошибка проверки webhook: %s", e, exc_info=True)

@app.on_event("shutdown")
async def on_shutdown():
    reminder_service.stop()
    await bot.session.close()
    logger.info("🛑 HTTP-сессии закрыты")

# === WEBHOOK ДЛЯ TELEGRAM ===
@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Точка входа для Telegram (Webhook)"""
    if config.WEBHOOK_SECRET_TOKEN:
        secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret_token != config.WEBHOOK_SECRET_TOKEN:
            logger.warning("⛔ Отклонён webhook-запрос с неверным secret token")
            raise HTTPException(status_code=403, detail="Invalid secret token")

    try:
        update_data = await request.json()
        update = types.Update.model_validate(update_data, context={"bot": bot})
        await dp.feed_update(bot, update)
        return {"status": "ok"}
    except Exception as e:
        logger.error("❌ Ошибка Webhook: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}

# === ЗАПУСК ===
if __name__ == "__main__":
    import uvicorn

    async def main():
        port = int(os.getenv("PORT", 8000))
        config_uv = uvicorn.Config(
            "app.main:app",
            host="0.0.0.0",
            port=port,
            log_level="info",
            reload=False,
            workers=1,
        )
        server = uvicorn.Server(config_uv)
        await server.serve()

    # Запускаем всё через единый event loop
    asyncio.run(main())