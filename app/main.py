import asyncio
import logging
import os
import re
from datetime import datetime, timezone

from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from fastapi import FastAPI, Request, HTTPException
from sqlalchemy.orm import sessionmaker

from app.core.config import config, validate_config
from app.core.budget_utils import (
    CURRENCY_CLARIFICATION_QUESTION,
    detect_currency,
    format_budget_with_currency,
    normalize_budget,
)
from app.core.database import engine, Base, ensure_schema
from app.core.llm_client import LLMClient
from app.core.telegram_utils import format_client_summary, format_manager_notification
from app.integrations.google_sheets import GoogleSheetsClient
from app.models.lead import Lead
from app.services.fsm_service import FSMService, LEAD_FIELDS, LeadField
from app.services.rate_limiter import RateLimiter
from app.services.reminder_service import ReminderService

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

PENDING_BUDGET_KEY = "budget_currency_pending"
REMINDER_ACTIVE_KEY = "reminder_active"
REMINDER_QUESTION_KEY = "reminder_question"
REMINDER_DELAY_KEY = "reminder_delay_seconds"
REMINDER_MAX_KEY = "reminder_max_reminders"
REMINDER_INTERVAL_KEY = "reminder_interval_seconds"
REMINDER_STARTED_AT_KEY = "reminder_started_at"
AWAITING_MANUAL_CONTACT_KEY = "awaiting_manual_contact"

COMPLETED_LEAD_HINT = (
    "✅ Ваша заявка уже принята. Менеджер свяжется с вами в ближайшее время.\n\n"
    "Чтобы оставить новую заявку, отправьте /new"
)

HELP_TEXT = (
    "ℹ️ <b>Справка по боту</b>\n\n"
    "Я помогаю собрать заявку на автомобиль под пригон из США.\n"
    "Отвечайте на вопросы по очереди — данные передадутся менеджеру.\n\n"
    "<b>Команды:</b>\n"
    "/start — начать работу с ботом\n"
    "/new — оставить новую заявку\n"
    "/cancel — отменить текущий диалог\n"
    "/help — показать эту справку"
)


def get_welcome_with_car_question() -> str:
    car_question = FSMService.get_question_for_field(LeadField.CAR)
    return (
        "👋 Здравствуйте! Я AI-агент для автобизнеса.\n\n"
        "Я помогу собрать заявку на автомобиль.\n"
        "Отвечайте на вопросы, и я передам данные менеджеру.\n\n"
        f"{car_question}"
    )


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


def has_pending_budget_currency(db, chat_id: str) -> bool:
    return bool(get_pending_state(db, chat_id).get(PENDING_BUDGET_KEY))


def get_pending_budget_currency(db, chat_id: str) -> str:
    return get_pending_state(db, chat_id).get(PENDING_BUDGET_KEY, "")


def set_pending_budget_currency(db, chat_id: str, amount: str) -> None:
    state = get_pending_state(db, chat_id)
    state[PENDING_BUDGET_KEY] = amount
    set_pending_state(db, chat_id, state)


def clear_pending_budget_currency(db, chat_id: str) -> None:
    state = get_pending_state(db, chat_id)
    state.pop(PENDING_BUDGET_KEY, None)
    set_pending_state(db, chat_id, state)


def is_awaiting_manual_contact(db, chat_id: str) -> bool:
    return bool(get_pending_state(db, chat_id).get(AWAITING_MANUAL_CONTACT_KEY))


def set_awaiting_manual_contact(db, chat_id: str, value: bool = True) -> None:
    state = get_pending_state(db, chat_id)
    if value:
        state[AWAITING_MANUAL_CONTACT_KEY] = True
    else:
        state.pop(AWAITING_MANUAL_CONTACT_KEY, None)
    set_pending_state(db, chat_id, state)


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
        "car": lead.car,
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
            [KeyboardButton(text="До 1 месяца")],
            [KeyboardButton(text="1-3 месяца")],
            [KeyboardButton(text="Более 3 месяцев")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_experience_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да, есть опыт")],
            [KeyboardButton(text="Нет, первый раз")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_currency_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="USD"), KeyboardButton(text="EUR"), KeyboardButton(text="BYN")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


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
def is_answer_valid(text: str, field_name: str) -> bool:
    """
    Проверяет, является ли ответ понятным для поля.
    Если ответ невнятный — вернет False, и бот покажет кнопки.
    """
    text_lower = text.lower().strip()
    
    if field_name == "budget":
        if not re.search(r'\d+', text):
            return False
        vague_words = ["сколько", "столько", "нормально", "хорошо", "норм", "как", "зависит"]
        if any(word in text_lower for word in vague_words):
            return False
        return True
        
    elif field_name == "timeline":
        time_words = ["месяц", "день", "недел", "год", "срочн", "быстр", "скоро", "сегодня", "завтра", "мес", "1-3", "3-6", "6+", ">6"]
        if not any(word in text_lower for word in time_words):
            return False
        return True
        
    elif field_name == "experience":
        experience_words = ["да", "нет", "первый", "есть", "не", "опыт", "ввоз", "покупал", "заказывал", "вроде", "помню"]
        if not any(word in text_lower for word in experience_words):
            return False
        return True
        
    elif field_name == "contact":
        # Очищаем от пробелов и спецсимволов
        cleaned_text = re.sub(r'[\s\(\)\-]', '', text)
        # Проверяем телефон: +375291015272 или 375291015272
        if re.search(r'\+?\d{10,15}', cleaned_text):
            return True
        # Проверяем @username
        if re.search(r'@\w+', text):
            return True
        # Если текст состоит только из цифр, пробелов, +, (, ), -
        if re.match(r'^[\d\s\+\(\)-]+$', text):
            return True
        return False
        
    return True

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_lead_data(lead: Lead) -> dict[str, str]:
    return {field: (getattr(lead, field) or "").strip() for field in LEAD_FIELDS}

def trim_dialog_history(dialog: list) -> list:
    if len(dialog) > config.MAX_DIALOG_ENTRIES:
        return dialog[-config.MAX_DIALOG_ENTRIES :]
    return dialog

def should_use_llm(lead_data: dict[str, str], text: str) -> bool:
    # Всегда вызываем LLM для длинных сообщений
    if len(text) > 50 or "\n" in text or text.count(",") >= 1:
        return True
    
    # Если есть хотя бы одно поле — проверяем, нужно ли обновление
    filled_count = sum(1 for field in LEAD_FIELDS if lead_data.get(field))
    if filled_count == 0:
        return True
    
    return False

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

def apply_parsed_fields(lead: Lead, parsed: dict[str, str]) -> None:
    for field in LEAD_FIELDS:
        value = parsed.get(field, "")
        if value:
            clean_value = clean_text(value)
            setattr(lead, field, clean_value)


def apply_budget_value(lead: Lead, chat_id: str, raw_budget: str, db) -> str | None:
    """
    Сохраняет бюджет с валютой.
    Возвращает текст уточняющего вопроса, если валюта не указана.
    """
    raw_budget = clean_text(raw_budget).strip()
    if not raw_budget:
        return None

    normalized, needs_currency = normalize_budget(raw_budget)
    if normalized:
        lead.budget = normalized
        clear_pending_budget_currency(db, chat_id)
        return None

    if needs_currency:
        set_pending_budget_currency(db, chat_id, raw_budget)
        lead.budget = ""
        return CURRENCY_CLARIFICATION_QUESTION

    lead.budget = raw_budget
    return None


def try_apply_pending_budget_currency(lead: Lead, chat_id: str, text: str, db) -> bool:
    """Применяет ответ с валютой к ожидающему бюджету. True — если обработано."""
    if not has_pending_budget_currency(db, chat_id):
        return False

    currency = detect_currency(text)
    if not currency:
        return False

    amount = get_pending_budget_currency(db, chat_id)
    clear_pending_budget_currency(db, chat_id)
    lead.budget = format_budget_with_currency(amount, currency)
    return True


async def send_reply(message: types.Message, text: str, **kwargs) -> None:
    """Отправляет ответ через bot.send_message (надёжно работает в webhook-режиме)."""
    chat_id = message.chat.id
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        logger.info("📤 Ответ отправлен в chat_id=%s", chat_id)
    except Exception as e:
        logger.error("❌ Ошибка отправки: %s", e, exc_info=True)
        raise


async def send_currency_clarification(message: types.Message) -> None:
    await send_reply(
        message,
        CURRENCY_CLARIFICATION_QUESTION,
        reply_markup=get_currency_keyboard(),
    )


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

    # Уведомление менеджеру
    if config.MANAGER_CHAT_ID:
        try:
            await bot.send_message(
                chat_id=int(config.MANAGER_CHAT_ID),
                text=format_manager_notification(lead),
                parse_mode=ParseMode.HTML,
            )
            logger.info("📨 Уведомление отправлено менеджеру: lead_id=%s", lead.id)
        except Exception as e:
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
    await send_reply(
        message,
        "👋 Здравствуйте! Я AI-агент для автобизнеса.\n\n"
        "Я помогу собрать заявку на автомобиль под пригон из США.\n"
        "Отвечайте на мои вопросы, и я передам данные менеджеру.\n\n"
        "Чтобы начать заявку, отправьте /new или просто напишите сообщение.",
    )


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
    chat_id = str(message.chat.id)
    username = message.from_user.username or "unknown"

    # === РЕЙТ-ЛИМИТ ===
    if not rate_limiter.is_allowed(chat_id):
        await send_reply(
            message,
            "⚠️ Слишком много сообщений. Подождите минуту и попробуйте снова.",
        )
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

    # === ОБРАБОТКА КОНТАКТА (ОТПРАВКА НОМЕРА) ===
    if message.contact:
        phone = message.contact.phone_number
        db = Session()
        try:
            lead = get_active_lead(db, chat_id)
            if lead and lead.status != "completed":
                clean_phone = clean_text(phone)
                setattr(lead, "contact", clean_phone)
                set_awaiting_manual_contact(db, chat_id, False)
                db.commit()
                logger.info(f"📱 Получен номер телефона: {clean_phone}")
                text = clean_phone
            else:
                await send_reply(message, "⚠️ Произошла ошибка. Попробуйте еще раз.")
                db.close()
                return
        except Exception as e:
            logger.error(f"❌ Ошибка обработки контакта: {e}")
            db.close()
            return
        finally:
            db.close()
    else:
        if not message.text:
            await send_reply(message, "Пожалуйста, отправьте текстовое сообщение.")
            return

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

    # === ЛОГИРОВАНИЕ ===
    logger.info("📩 Сообщение от chat_id=%s, длина=%s", chat_id, len(text))

    db = Session()

    try:
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
            welcome_text = get_welcome_with_car_question()
            await send_reply(message, welcome_text)
            just_sent_welcome = True
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

        # 3. ПОЛУЧАЕМ ТЕКУЩИЕ ДАННЫЕ
        lead_data = get_lead_data(lead)
        expected_field = FSMService.get_next_field(lead_data)

        # 3.1 УТОЧНЕНИЕ ВАЛЮТЫ ДЛЯ БЮДЖЕТА
        if has_pending_budget_currency(db, chat_id):
            if try_apply_pending_budget_currency(lead, chat_id, text, db):
                logger.info(
                    "💱 Бюджет с валютой сохранён для chat_id=%s: %s",
                    chat_id,
                    lead.budget,
                )
            else:
                await send_currency_clarification(message)
                return

        # 4. ЕСЛИ ЕСТЬ ОЖИДАЕМОЕ ПОЛЕ — ПРОВЕРЯЕМ ОТВЕТ
        if expected_field and expected_field.value in ["budget", "timeline", "experience", "contact"]:
            field_name = expected_field.value
            is_valid = is_answer_valid(text, field_name)

            if (
                field_name == "contact"
                and is_awaiting_manual_contact(db, chat_id)
                and text.strip()
            ):
                is_valid = True
            
            if not is_valid:
                keyboard = None if (
                    field_name == "contact" and is_awaiting_manual_contact(db, chat_id)
                ) else get_keyboard_for_field(field_name)
                if keyboard:
                    await send_reply(
                        message,
                        "🤔 Не совсем понял. Пожалуйста, уточните, выбрав вариант:",
                        reply_markup=keyboard,
                    )
                    logger.info(f"❓ Показаны варианты для поля {field_name}, chat_id={chat_id}")
                    return
                else:
                    await send_reply(
                        message,
                        f"🤔 Не совсем понял. Пожалуйста, уточните ответ для поля '{field_name}'.",
                    )
                    return

        # 5. ПАРСИНГ (если нужно)
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
            pass
        elif should_use_llm(lead_data, text):
            parsed = await llm.parse_message(text)
            budget_clarification = None
            if parsed.get("budget"):
                budget_clarification = apply_budget_value(lead, chat_id, parsed["budget"], db)
                parsed["budget"] = ""
            apply_parsed_fields(lead, parsed)
            logger.info("🧠 LLM-парсинг выполнен для lead_id=%s", lead.id)
            lead_data = get_lead_data(lead)
            logger.info("📋 Данные после LLM: %s", lead_data)
            if budget_clarification:
                dialog = list(lead.dialog_history or [])
                dialog.append(
                    {
                        "role": "user",
                        "text": text,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                lead.dialog_history = trim_dialog_history(dialog)
                await send_currency_clarification(message)
                dialog.append(
                    {
                        "role": "assistant",
                        "text": budget_clarification,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                lead.dialog_history = trim_dialog_history(dialog)
                db.commit()
                return
        elif expected_field:
            if expected_field.value == "budget":
                budget_clarification = apply_budget_value(lead, chat_id, text, db)
                logger.info(
                    "📝 Поле budget обработано для lead_id=%s: %s",
                    lead.id,
                    lead.budget or f"ожидает валюту ({get_pending_budget_currency(db, chat_id)})",
                )
                if budget_clarification:
                    dialog = list(lead.dialog_history or [])
                    dialog.append(
                        {
                            "role": "user",
                            "text": text,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    lead.dialog_history = trim_dialog_history(dialog)
                    await send_currency_clarification(message)
                    dialog.append(
                        {
                            "role": "assistant",
                            "text": budget_clarification,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    lead.dialog_history = trim_dialog_history(dialog)
                    db.commit()
                    return
            else:
                cleaned_value = clean_text(text)
                setattr(lead, expected_field.value, cleaned_value)
                if expected_field.value == "contact":
                    set_awaiting_manual_contact(db, chat_id, False)
                logger.info(
                    "📝 Поле %s заполнено напрямую для lead_id=%s: %s",
                    expected_field.value,
                    lead.id,
                    cleaned_value,
                )

        # 6. СОХРАНЕНИЕ ИСТОРИИ
        dialog = list(lead.dialog_history or [])
        dialog.append(
            {
                "role": "user",
                "text": text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        lead.dialog_history = trim_dialog_history(dialog)

        # 7. ОПРЕДЕЛЕНИЕ СЛЕДУЮЩЕГО ВОПРОСА
        lead_data = get_lead_data(lead)
        next_field = FSMService.get_next_field(lead_data)

        if next_field:
            if just_sent_welcome and next_field == LeadField.CAR:
                return

            question = FSMService.get_question_for_field(next_field)
            reply_markup = None
            if next_field == LeadField.CONTACT and not is_awaiting_manual_contact(db, chat_id):
                reply_markup = get_contact_keyboard()
            elif next_field == LeadField.BUDGET:
                reply_markup = get_budget_keyboard()
            elif next_field == LeadField.TIMELINE:
                reply_markup = get_timeline_keyboard()
            elif next_field == LeadField.EXPERIENCE:
                reply_markup = get_experience_keyboard()

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

    if config.WEBHOOK_SECRET_TOKEN:
        logger.info(
            "🔐 WEBHOOK_SECRET_TOKEN задан. Установите webhook с параметром secret_token: "
            "https://api.telegram.org/bot<TOKEN>/setWebhook?url=%s&secret_token=<WEBHOOK_SECRET_TOKEN>",
            config.WEBHOOK_URL,
        )
    else:
        logger.warning(
            "⚠️ WEBHOOK_SECRET_TOKEN не задан. Эндпоинт /webhook/telegram принимает запросы без проверки."
        )

    logger.info("⏳ Webhook не установлен автоматически, установи вручную через браузер")

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