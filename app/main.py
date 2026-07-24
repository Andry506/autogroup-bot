import asyncio
import logging
import os
import re
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from fastapi import FastAPI, Request, HTTPException
from sqlalchemy.orm import sessionmaker
from app.core.config import config, validate_config
from app.core.car_validation import (
    car_to_db,
    format_car_display,
    is_car_answer_valid,
    is_car_filled,
    parse_car_answer,
    parse_car_hybrid,
    strip_greetings_from_car_text,
)
from app.core.contact_validation import (
    INVALID_COUNTRY_CODE_MESSAGE,
    is_phone_number_valid,
    is_valid_contact,
    normalize_contact,
)
from app.core.contact_validation import (
    INVALID_COUNTRY_CODE_MESSAGE,
    is_phone_number_valid,
    is_valid_contact,
    normalize_contact,
)
from app.core.contact_validation import is_valid_contact
from app.core.database import engine, Base, ensure_schema
from app.core.llm_client import LLMClient, fallback_message, is_empty_parsed
from app.core.options import BUDGET_OPTIONS, MARKET_OPTIONS, TIMELINE_OPTIONS
from app.core.telegram_utils import (
    format_client_summary,
    format_manager_notification,
    format_manager_update_notification,
)
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
EDIT_MODE_KEY = "edit_mode"
EDITING_FIELD_KEY = "editing_field"
AWAITING_CONSENT_KEY = "awaiting_consent"
CONSENT_DECLINED_KEY = "consent_declined"

ACCEPT_CONSENT_CALLBACK = "accept_consent"
DECLINE_CONSENT_CALLBACK = "decline_consent"

CONSENT_PROMPT_TEXT = (
    "Здравствуйте! Я AI-помощник компании AutoGroup!\n"
    "Я помогу собрать заявку на автомобиль \"мечты\"!\n"
    "Пожалуйста, отвечайте на вопросы, и я передам данные менеджеру.\n\n"
    "Для начала работы необходимо принять условия обработки персональных данных.\n"
    "Полный текст соглашения доступен по ссылке: "
    "https://telegra.ph/SOGLASIE-NA-OBRABOTKU-PERSONALNYH-DANNYH-PRI-ISPOLZOVANII-CHAT-BOTA-07-24\n\n"
    "Нажимая кнопку «Согласен(а)», вы подтверждаете, что ознакомлены и согласны с условиями.\n"
    "Если вы не согласны — нажмите «Не согласен». "
    "Без вашего согласия мы не сможем продолжить работу."
)

CONSENT_REQUIRED_MESSAGE = (
    "Для начала работы, пожалуйста, примите условия обработки персональных данных, "
    "нажав кнопку «Согласен(а)» в сообщении выше."
)

CONSENT_DECLINED_MESSAGE = (
    "К сожалению, без вашего согласия на обработку персональных данных "
    "мы не можем продолжить работу. Если передумаете — просто нажмите /start, "
    "и мы начнём заново."
)

CONSENT_ACCEPTED_MESSAGE = (
    "Спасибо! Теперь вы можете продолжить.\n"
    "🚗 Какой автомобиль Вас интересует?"
)


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

FIELD_MENU_LABELS = {
    "car": "Автомобиль",
    "budget": "Бюджет",
    "timeline": "Срок",
    "experience": "Рынок",
    "contact": "Контакт",
}

FIELD_MENU_TO_NAME = {label: name for name, label in FIELD_MENU_LABELS.items()}

VALIDATED_FIELDS = ["car", "budget", "timeline", "experience", "contact"]
BUTTON_FIELDS = frozenset({"budget", "timeline", "experience"})

YES_CONFIRM_WORDS = ["да", "yes", "ага", "верно", "подтверждаю", "ок", "ok"]
NO_CONFIRM_WORDS = ["нет", "no", "неа", "оставь", "отмена", "не надо"]


COMPLETED_LEAD_HINT = (
    "✅ Ваша заявка уже принята. Менеджер свяжется с Вами в ближайшее время.\n\n"
    "Чтобы оставить новую заявку, отправьте /new.\n"
    "Чтобы изменить данные, нажмите «Изменить заявку»."
)

POST_COMPLETION_HINT = (
    "Если хотите изменить данные в заявке — нажмите на кнопку «Изменить заявку» ниже.\n"
    "Если хотите начать новую заявку — нажмите «Новая заявка»."
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
    "/change — изменить уже данный ответ\n"
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


def get_new_application_start_text() -> str:
    car_question = FSMService.get_question_for_field(LeadField.CAR)
    return f"Конечно! Давайте создадим новую заявку.\n\n{car_question}"


def get_active_lead(db, chat_id: str) -> Lead | None:
    """Возвращает незавершённую заявку пользователя."""
    return (
        db.query(Lead)
        .filter(Lead.chat_id == chat_id, Lead.status != "completed")
        .order_by(Lead.created_at.desc())
        .first()
    )


def get_latest_completed_lead(db, chat_id: str) -> Lead | None:
    return (
        db.query(Lead)
        .filter(Lead.chat_id == chat_id, Lead.status == "completed")
        .order_by(Lead.created_at.desc())
        .first()
    )


def get_working_lead(db, chat_id: str) -> Lead | None:
    active = get_active_lead(db, chat_id)
    if active:
        return active

    completed = get_latest_completed_lead(db, chat_id)
    if completed and (completed.pending_state or {}).get(EDIT_MODE_KEY):
        return completed
    return None


def user_has_consent(db, chat_id: str) -> bool:
    """Проверяет, давал ли пользователь согласие на обработку ПДн (по chat_id)."""
    return (
        db.query(Lead)
        .filter(Lead.chat_id == chat_id, Lead.consent_given.is_(True))
        .count()
        > 0
    )


def is_awaiting_consent(lead: Lead | None) -> bool:
    if not lead:
        return False
    state = get_lead_pending_state(lead)
    return bool(state.get(AWAITING_CONSENT_KEY) or state.get(CONSENT_DECLINED_KEY))


def set_consent_pending_flags(
    lead: Lead,
    *,
    awaiting: bool = False,
    declined: bool = False,
) -> None:
    state = get_lead_pending_state(lead)
    if awaiting:
        state[AWAITING_CONSENT_KEY] = True
    else:
        state.pop(AWAITING_CONSENT_KEY, None)
    if declined:
        state[CONSENT_DECLINED_KEY] = True
    else:
        state.pop(CONSENT_DECLINED_KEY, None)
    lead.pending_state = state


def mark_consent_accepted(lead: Lead) -> None:
    lead.consent_given = True
    lead.consent_given_at = datetime.now(timezone.utc)
    set_consent_pending_flags(lead, awaiting=False, declined=False)


def get_consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Согласен(а)",
                    callback_data=ACCEPT_CONSENT_CALLBACK,
                ),
                InlineKeyboardButton(
                    text="❌ Не согласен",
                    callback_data=DECLINE_CONSENT_CALLBACK,
                ),
            ]
        ]
    )


async def send_consent_prompt(message: types.Message, db, lead: Lead) -> None:
    """Отправляет текст согласия с инлайн-кнопками и сохраняет awaiting_consent."""
    set_consent_pending_flags(lead, awaiting=True, declined=False)
    dialog = list(lead.dialog_history or [])
    dialog.append(
        {
            "role": "assistant",
            "text": CONSENT_PROMPT_TEXT,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    lead.dialog_history = trim_dialog_history(dialog)
    db.commit()
    await send_reply(
        message,
        CONSENT_PROMPT_TEXT,
        reply_markup=get_consent_keyboard(),
        disable_web_page_preview=True,
    )


def get_lead_pending_state(lead: Lead | None) -> dict:
    if not lead or not lead.pending_state:
        return {}
    return dict(lead.pending_state)


def get_expected_field(db, chat_id: str, lead: Lead) -> LeadField | None:
    state = get_lead_pending_state(lead)
    editing_field = state.get(EDITING_FIELD_KEY)
    if editing_field:
        try:
            return LeadField(editing_field)
        except ValueError:
            pass
    return FSMService.get_next_field(get_lead_data(lead))


def set_edit_mode(db, lead: Lead, enabled: bool = True) -> None:
    state = get_lead_pending_state(lead)
    if enabled:
        state[EDIT_MODE_KEY] = True
    else:
        state.pop(EDIT_MODE_KEY, None)
        state.pop(EDITING_FIELD_KEY, None)
    lead.pending_state = state


def start_field_edit(db, chat_id: str, lead: Lead, field_name: str) -> None:
    state = get_lead_pending_state(lead)
    state[EDIT_MODE_KEY] = True
    state[EDITING_FIELD_KEY] = field_name
    lead.pending_state = state
    cancel_reminder_for_chat(db, chat_id)


def clear_field_edit(db, chat_id: str, lead: Lead) -> None:
    state = get_lead_pending_state(lead)
    state.pop(EDITING_FIELD_KEY, None)
    lead.pending_state = state


def get_filled_fields(lead_data: dict[str, str]) -> list[str]:
    return [field for field in LEAD_FIELDS if lead_data.get(field)]


def get_pending_state(db, chat_id: str, lead: Lead | None = None) -> dict:
    """Возвращает pending_state рабочего лида из БД."""
    lead = lead or get_working_lead(db, chat_id)
    return get_lead_pending_state(lead)


def set_pending_state(db, chat_id: str, state: dict | None, lead: Lead | None = None) -> None:
    """Сохраняет pending_state рабочего лида в БД."""
    lead = lead or get_working_lead(db, chat_id)
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


def get_status_rus(status: str) -> str:
    if status == "completed":
        return "Завершена"
    return status


def build_lead_row(lead: Lead) -> dict:
    return {
        "chat_id": lead.chat_id,
        "username": lead.username,
        "car": format_car_display(lead.car),
        "budget": lead.budget,
        "timeline": lead.timeline,
        "experience": lead.experience,
        "contact": lead.contact,
        "status": get_status_rus(lead.status),
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
            [KeyboardButton(text="Новая заявка")],
            [KeyboardButton(text="Изменить заявку")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_edit_fields_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Автомобиль"), KeyboardButton(text="Бюджет")],
            [KeyboardButton(text="Срок"), KeyboardButton(text="Рынок")],
            [KeyboardButton(text="Контакт")],
            [KeyboardButton(text="Готово")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_change_fields_keyboard(lead_data: dict[str, str]) -> ReplyKeyboardMarkup | None:
    filled = get_filled_fields(lead_data)
    if not filled:
        return None

    rows: list[list[KeyboardButton]] = []
    current_row: list[KeyboardButton] = []
    for field_name in filled:
        current_row.append(KeyboardButton(text=f"Изменить {FIELD_MENU_LABELS[field_name].lower()}"))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)

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
    text_stripped = text.strip()

    if field_name == "car":
        return is_car_answer_valid(strip_greetings_from_car_text(text_stripped))

    if field_name == "budget":
        return text_stripped in BUDGET_OPTIONS

    if field_name == "timeline":
        return text_stripped in TIMELINE_OPTIONS

    if field_name == "experience":
        return text_stripped in MARKET_OPTIONS

    if field_name == "contact":

        if re.search(r"@\w+", text_stripped):
            return True
        return is_phone_number_valid(text_stripped)

    return True


def get_contact_error_message(text: str) -> str:
    ok, _, error = normalize_contact(text)
    if ok:
        return get_invalid_answer_message("contact")
    if error == INVALID_COUNTRY_CODE_MESSAGE:
        return INVALID_COUNTRY_CODE_MESSAGE
    return error or get_invalid_answer_message("contact")


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
            "Пожалуйста, введите корректный номер телефона "
            "(например, +375291234567) или @username в Telegram."
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
    return is_phone_number_valid(text)


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
        ok, value, _ = normalize_contact(text)
        if not ok:
            return False
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
            car_result = await parse_car_answer(strip_greetings_from_car_text(clean_value), llm)
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

        if field == "contact":
            ok, normalized, _ = normalize_contact(clean_value)
            if not ok:
                continue
            clean_value = normalized

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
    if include_welcome:
        display_text = get_welcome_with_car_question()
    else:
        display_text = get_new_application_start_text()

    car_question = FSMService.get_question_for_field(LeadField.CAR)

    await send_reply(message, display_text)
    await schedule_question_reminder(db, chat_id, car_question)

    dialog = list(lead.dialog_history or [])
    dialog.append(
        {
            "role": "assistant",
            "text": display_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    lead.dialog_history = trim_dialog_history(dialog)
    db.commit()
    logger.info("🆕 Начат диалог для lead_id=%s", lead.id)

async def notify_manager(lead: Lead, *, is_update: bool = False) -> None:
    """Отправляет менеджеру/в группу уведомление о новой заявке или об её изменении."""
    notification_chat_id = config.GROUP_CHAT_ID or config.MANAGER_CHAT_ID
    if not notification_chat_id:
        return

    text = (
        format_manager_update_notification(lead)
        if is_update
        else format_manager_notification(lead)
    )
    kwargs = {
        "chat_id": int(notification_chat_id),
        "text": text,
        "parse_mode": ParseMode.HTML,
    }

    reply_to_id = lead.manager_notification_message_id if is_update else None
    if reply_to_id:
        kwargs["reply_to_message_id"] = int(reply_to_id)

    target = "в группу" if config.GROUP_CHAT_ID else "менеджеру"

    try:
        sent = await bot.send_message(**kwargs)
        if not is_update:
            lead.manager_notification_message_id = sent.message_id
        logger.info(
            "📨 Уведомление%s отправлено %s: lead_id=%s, reply_to=%s",
            " об изменении" if is_update else "",
            target,
            lead.id,
            reply_to_id,
        )
    except Exception as e:
        # Для старых/удалённых сообщений повторяем без reply
        if is_update and reply_to_id:
            logger.warning(
                "⚠️ Reply на исходное уведомление не удался (lead_id=%s, message_id=%s): %s. "
                "Отправляю без reply.",
                lead.id,
                reply_to_id,
                e,
            )
            kwargs.pop("reply_to_message_id", None)
            try:
                await bot.send_message(**kwargs)
                logger.info(
                    "📨 Уведомление об изменении отправлено без reply: lead_id=%s",
                    lead.id,
                )
            except Exception as retry_error:
                logger.error(
                    "❌ Ошибка отправки уведомления об изменении %s: %s",
                    target,
                    retry_error,
                )
        else:
            logger.error("❌ Ошибка отправки уведомления %s: %s", target, e)

async def finish_completed_field_edit(lead: Lead, message: types.Message, db) -> None:
    """Сохраняет изменения в завершённой заявке и обновляет экспорт."""
    clear_field_edit(db, str(message.chat.id), lead)
    lead.export_status = "pending"
    db.commit()

    try:
        saved = await asyncio.to_thread(export_lead_to_sheets, lead)
        lead.export_status = "exported" if saved else "failed"
    except Exception as e:
        lead.export_status = "failed"
        logger.error("❌ Ошибка повторного экспорта после редактирования: %s", e)

    # Уведомляем менеджера об изменении (reply на исходное уведомление, если есть)
    await notify_manager(lead, is_update=True)
    db.commit()

    await send_reply(message, "✅ Данные заявки обновлены.")
    await send_reply(
        message,
        format_client_summary(lead),
        parse_mode=ParseMode.HTML,
    )
    await send_reply(
        message,
        POST_COMPLETION_HINT,
        reply_markup=get_new_application_keyboard(),
    )


async def prompt_field_edit(
    message: types.Message,
    db,
    lead: Lead,
    field_name: str,
) -> None:
    chat_id = str(message.chat.id)
    start_field_edit(db, chat_id, lead, field_name)
    field = LeadField(field_name)
    question = FSMService.get_question_for_field(field)
    await send_reply(
        message,
        f"Введите новое значение для поля «{FIELD_MENU_LABELS[field_name]}»:\n\n{question}",
        reply_markup=get_reply_markup_for_field(db, chat_id, field),
    )
    db.commit()

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
        POST_COMPLETION_HINT,
        reply_markup=get_new_application_keyboard(),
    )

    # Уведомление менеджеру/в группу (сохраняем message_id для будущих reply)
    await notify_manager(lead, is_update=False)

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
        has_consent = user_has_consent(db, chat_id)

        if not has_consent:
            active_lead = get_active_lead(db, chat_id)
            if active_lead and is_awaiting_consent(active_lead):
                lead = active_lead
            elif active_lead and not active_lead.consent_given:
                lead = active_lead
            else:
                cancel_reminder_for_chat(db, chat_id)
                lead = Lead(chat_id=chat_id, username=username, pending_state={})
                db.add(lead)
                db.flush()
            if username != "unknown":
                lead.username = username
            await send_consent_prompt(message, db, lead)
            logger.info(
                "👋 /start: запрос согласия lead_id=%s chat_id=%s",
                lead.id,
                chat_id,
            )
            return

        has_completed = (
            db.query(Lead)
            .filter(Lead.chat_id == chat_id, Lead.status == "completed")
            .count()
            > 0
        )
        if has_completed:
            await send_reply(
                message,
                COMPLETED_LEAD_HINT,
                reply_markup=get_new_application_keyboard(),
            )
            return

        active_lead = get_active_lead(db, chat_id)
        if active_lead:
            if not active_lead.consent_given:
                # Переносим факт согласия с другого лида того же chat_id
                mark_consent_accepted(active_lead)
                db.commit()
            await send_reply(
                message,
                "У вас уже есть активная заявка. Продолжайте диалог или отправьте /cancel.",
            )
            return

        cancel_reminder_for_chat(db, chat_id)
        lead = Lead(
            chat_id=chat_id,
            username=username,
            pending_state={},
            consent_given=True,
            consent_given_at=datetime.now(timezone.utc),
        )
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


@dp.callback_query(lambda c: c.data in {ACCEPT_CONSENT_CALLBACK, DECLINE_CONSENT_CALLBACK})
async def handle_consent_callback(callback: CallbackQuery):
    chat_id = str(callback.message.chat.id) if callback.message else str(callback.from_user.id)
    db = Session()
    try:
        lead = get_active_lead(db, chat_id)
        if not lead:
            await callback.answer("Сначала нажмите /start", show_alert=True)
            return

        if lead.consent_given:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.answer("Согласие уже принято")
            return

        if callback.data == ACCEPT_CONSENT_CALLBACK:
            mark_consent_accepted(lead)
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.answer()

            car_question = FSMService.get_question_for_field(LeadField.CAR)
            display_text = CONSENT_ACCEPTED_MESSAGE
            await bot.send_message(chat_id=int(chat_id), text=display_text)
            await schedule_question_reminder(db, chat_id, car_question)

            dialog = list(lead.dialog_history or [])
            dialog.append(
                {
                    "role": "assistant",
                    "text": display_text,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            lead.dialog_history = trim_dialog_history(dialog)
            db.commit()
            logger.info("✅ Согласие принято: lead_id=%s chat_id=%s", lead.id, chat_id)
            return

        # decline_consent
        set_consent_pending_flags(lead, awaiting=False, declined=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer()
        await bot.send_message(chat_id=int(chat_id), text=CONSENT_DECLINED_MESSAGE)
        dialog = list(lead.dialog_history or [])
        dialog.append(
            {
                "role": "assistant",
                "text": CONSENT_DECLINED_MESSAGE,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        lead.dialog_history = trim_dialog_history(dialog)
        db.commit()
        logger.info("❌ Согласие отклонено: lead_id=%s chat_id=%s", lead.id, chat_id)
    except Exception as e:
        logger.error("❌ Ошибка обработки согласия: %s", e, exc_info=True)
        await callback.answer("Произошла ошибка. Попробуйте /start", show_alert=True)
        db.rollback()
    finally:
        db.close()


async def ensure_consent_or_reply(message: types.Message, db, chat_id: str) -> bool:
    """Возвращает True, если согласие есть. Иначе отвечает и возвращает False."""
    if user_has_consent(db, chat_id):
        return True
    await send_reply(message, CONSENT_REQUIRED_MESSAGE)
    return False


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    chat_id = str(message.chat.id)
    db = Session()
    try:
        if not await ensure_consent_or_reply(message, db, chat_id):
            return
        await send_reply(message, HELP_TEXT, parse_mode=ParseMode.HTML)
    finally:
        db.close()


FIELD_CHANGE_ALIASES = {
    "car": "car",
    "авто": "car",
    "автомобиль": "car",
    "budget": "budget",
    "бюджет": "budget",
    "timeline": "timeline",
    "срок": "timeline",
    "experience": "experience",
    "рынок": "experience",
    "contact": "contact",
    "контакт": "contact",
}

CHANGE_BUTTON_TO_FIELD = {
    f"изменить {FIELD_MENU_LABELS[name].lower()}": name
    for name in FIELD_MENU_LABELS
}


@dp.message(Command("change"))
async def cmd_change(message: types.Message, command: CommandObject):
    chat_id = str(message.chat.id)
    db = Session()
    try:
        if not await ensure_consent_or_reply(message, db, chat_id):
            return

        lead = get_working_lead(db, chat_id) or get_active_lead(db, chat_id)
        if not lead:
            lead = get_latest_completed_lead(db, chat_id)
            if lead:
                set_edit_mode(db, lead, True)
            else:
                await send_reply(message, "Сначала начните заявку командой /start.")
                return

        field_arg = (command.args or "").strip().lower()
        if field_arg:
            field_name = FIELD_CHANGE_ALIASES.get(field_arg)
            if not field_name:
                await send_reply(
                    message,
                    "Неизвестное поле. Используйте: car, budget, timeline, experience, contact.",
                )
                return
            await prompt_field_edit(message, db, lead, field_name)
            return

        lead_data = get_lead_data(lead)
        keyboard = get_change_fields_keyboard(lead_data)
        if not keyboard:
            await send_reply(message, "Пока нет заполненных полей для изменения.")
            return

        await send_reply(
            message,
            "Какое поле вы хотите изменить?",
            reply_markup=keyboard,
        )
        db.commit()
    except Exception as e:
        logger.error("❌ Ошибка команды /change: %s", e, exc_info=True)
        await send_reply(message, "⚠️ Не удалось начать изменение. Попробуйте позже.")
        db.rollback()
    finally:
        db.close()


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
        if not await ensure_consent_or_reply(message, db, chat_id):
            return

        active_lead = get_active_lead(db, chat_id)
        if active_lead:
            await send_reply(
                message,
                "У вас уже есть активная заявка. Продолжайте диалог или отправьте /cancel.",
            )
            return

        cancel_reminder_for_chat(db, chat_id)
        lead = Lead(
            chat_id=chat_id,
            username=username,
            pending_state={},
            consent_given=True,
            consent_given_at=datetime.now(timezone.utc),
        )
        db.add(lead)
        db.flush()
        await begin_lead_dialog(message, db, lead, include_welcome=False)
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

    # === СОГЛАСИЕ НА ОБРАБОТКУ ПДн ===
    consent_db = Session()
    try:
        if not user_has_consent(consent_db, chat_id):
            await send_reply(message, CONSENT_REQUIRED_MESSAGE)
            return
    finally:
        consent_db.close()

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
    if message.text and message.text.strip() in {"/new", "Новая заявка", "Оставить новую заявку"}:
        await cmd_new(message)
        return

    # === РЕДАКТИРОВАНИЕ ЗАВЕРШЁННОЙ ЗАЯВКИ ===
    if message.text:
        text_action = message.text.strip()
        if text_action in {"Изменить заявку", "Изменить данные заявки"}:
            db = Session()
            try:
                lead = get_latest_completed_lead(db, chat_id)
                if not lead:
                    await send_reply(message, "Сначала завершите заявку.")
                    return
                set_edit_mode(db, lead, True)
                await send_reply(
                    message,
                    "Выберите поле, которое хотите изменить:",
                    reply_markup=get_edit_fields_keyboard(),
                )
                db.commit()
            finally:
                db.close()
            return

        if text_action == "Готово":
            db = Session()
            try:
                lead = get_working_lead(db, chat_id)
                if lead and (lead.pending_state or {}).get(EDIT_MODE_KEY):
                    set_edit_mode(db, lead, False)
                    db.commit()
                    await send_reply(
                        message,
                        "Редактирование завершено.",
                        reply_markup=get_new_application_keyboard(),
                    )
                else:
                    await send_reply(message, "Нет активного режима редактирования.")
            finally:
                db.close()
            return

        field_name = FIELD_MENU_TO_NAME.get(text_action)
        if not field_name:
            field_name = CHANGE_BUTTON_TO_FIELD.get(text_action.lower())

        if field_name:
            db = Session()
            try:
                lead = get_working_lead(db, chat_id) or get_latest_completed_lead(db, chat_id)
                if not lead:
                    await send_reply(message, "Сначала начните или завершите заявку.")
                    return
                if lead.status == "completed":
                    set_edit_mode(db, lead, True)
                await prompt_field_edit(message, db, lead, field_name)
            finally:
                db.close()
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

        lead = get_working_lead(db, chat_id)
        is_new_lead = False
        just_sent_welcome = False

        if not lead:
            completed = get_latest_completed_lead(db, chat_id)
            if completed and not (completed.pending_state or {}).get(EDIT_MODE_KEY):
                await send_reply(
                    message,
                    COMPLETED_LEAD_HINT,
                    reply_markup=get_new_application_keyboard(),
                )
                return

            lead = Lead(
                chat_id=chat_id,
                username=username,
                pending_state={},
                consent_given=True,
                consent_given_at=datetime.now(timezone.utc),
            )
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
        expected_field = get_expected_field(db, chat_id, lead)
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
            phone = clean_text(shared_contact.phone_number)
            ok, formatted_phone, error = normalize_contact(phone)
            if not ok:
                await send_reply(message, error or get_contact_error_message(phone))
                return
            lead.contact = formatted_phone
            set_awaiting_manual_contact(db, chat_id, False)
            skip_field_processing = True
            logger.info("📱 Получен номер телефона: %s", lead.contact)
            if get_lead_pending_state(lead).get(EDITING_FIELD_KEY) == "contact":
                if lead.status == "completed":
                    await finish_completed_field_edit(lead, message, db)
                    return
                clear_field_edit(db, chat_id, lead)
                await send_reply(message, "✅ Поле «Контакт» обновлено.")
                db.commit()
                expected_field = get_expected_field(db, chat_id, lead)
                if expected_field:
                    await send_current_field_prompt(message, db, chat_id, expected_field)
                return
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
                    car_text = strip_greetings_from_car_text(text)
                    car_parse_result = await parse_car_answer(car_text, llm)
                    is_valid = car_parse_result.get("status") == "ok"
                else:
                    is_valid = is_answer_valid(text, field_name)

                if not is_valid:
                    keyboard = None if (
                        field_name == "contact" and is_awaiting_manual_contact(db, chat_id)
                    ) else get_keyboard_for_field(field_name)
                    message_text = (
                        get_contact_error_message(text)
                        if field_name == "contact"
                        else get_invalid_answer_message(field_name)
                    )
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

                if field_answer_applied:
                    editing_field = get_lead_pending_state(lead).get(EDITING_FIELD_KEY)
                    if editing_field:
                        if lead.status == "completed":
                            await finish_completed_field_edit(lead, message, db)
                            return
                        clear_field_edit(db, chat_id, lead)
                        await send_reply(
                            message,
                            f"✅ Поле «{FIELD_MENU_LABELS[expected_field.value]}» обновлено.",
                        )
                        db.commit()
                        expected_field = get_expected_field(db, chat_id, lead)
                        if expected_field:
                            await send_current_field_prompt(message, db, chat_id, expected_field)
                            return

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
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Начать работу с ботом"),
                BotCommand(command="new", description="Оставить новую заявку"),
                BotCommand(command="change", description="Изменить ответ"),
                BotCommand(command="cancel", description="Отменить диалог"),
                BotCommand(command="help", description="Справка"),
            ]
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