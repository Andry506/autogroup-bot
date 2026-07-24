import html

from app.core.car_validation import format_car_display
from app.core.contact_validation import format_manager_contact, format_manager_username
from app.models.lead import Lead


def escape_html(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def format_client_summary(lead: Lead) -> str:
    return (
        "✅ <b>Спасибо! Заявка принята!</b>\n\n"
        "📋 <b>Ваши данные:</b>\n"
        f"🚗 <b>Авто:</b> {escape_html(format_car_display(lead.car))}\n"
        f"💰 <b>Бюджет:</b> {escape_html(lead.budget)}\n"
        f"📅 <b>Срок:</b> {escape_html(lead.timeline)}\n"
        f"🌍 <b>Рынок:</b> {escape_html(lead.experience)}\n"
        f"📱 <b>Контакт:</b> {escape_html(lead.contact)}\n\n"
        "👨‍💼 Менеджер свяжется с Вами в ближайшее время!"
    )


def _format_manager_lead_fields(lead: Lead) -> str:
    return (
        f"Авто: {format_car_display(lead.car)}\n"
        f"Бюджет: {lead.budget}\n"
        f"Срок: {lead.timeline}\n"
        f"Рынок: {lead.experience}\n"
        f"Контакт: {format_manager_contact(lead.contact)}\n"
        f"Username: {format_manager_username(lead.username)}\n"
        f"Chat ID: {lead.chat_id}\n\n"
        "Свяжитесь с клиентом по Chat ID в Telegram или по номеру телефона, указанному в заявке."
    )


def format_manager_notification(lead: Lead) -> str:
    """Форматирует уведомление для менеджера о новой заявке."""
    return f"НОВАЯ ЗАЯВКА!\n\nДанные клиента:\n{_format_manager_lead_fields(lead)}"


def format_manager_update_notification(lead: Lead) -> str:
    """Форматирует уведомление для менеджера об изменении завершённой заявки."""
    return (
        "🔄 ЗАЯВКА ИЗМЕНЕНА\n\n"
        f"Обновлённые данные клиента:\n{_format_manager_lead_fields(lead)}"
    )
