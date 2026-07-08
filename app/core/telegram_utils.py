import html

from app.models.lead import Lead


def escape_html(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def format_client_summary(lead: Lead) -> str:
    return (
        "✅ <b>Спасибо! Заявка принята!</b>\n\n"
        "📋 <b>Ваши данные:</b>\n"
        f"🚗 <b>Авто:</b> {escape_html(lead.car)}\n"
        f"💰 <b>Бюджет:</b> {escape_html(lead.budget)}\n"
        f"📅 <b>Срок:</b> {escape_html(lead.timeline)}\n"
        f"📦 <b>Опыт:</b> {escape_html(lead.experience)}\n"
        f"📱 <b>Контакт:</b> {escape_html(lead.contact)}\n\n"
        "👨‍💼 Менеджер свяжется с вами в ближайшее время!"
    )


def format_manager_notification(lead: Lead) -> str:
    """Форматирует уведомление для менеджера (БЕЗ ЭМОДЗИ)"""
    return (
        "НОВАЯ ЗАЯВКА!\n\n"
        "Данные клиента:\n"
        f"Авто: {lead.car}\n"
        f"Бюджет: {lead.budget}\n"
        f"Срок: {lead.timeline}\n"
        f"Опыт: {lead.experience}\n"
        f"Контакт: {lead.contact}\n"
        f"Username: @{lead.username or 'не указан'}\n"
        f"Chat ID: {lead.chat_id}\n\n"
        "Свяжитесь с клиентом в Telegram по Chat ID."
    )