"""Тесты форматирования уведомлений менеджеру."""

import unittest
from types import SimpleNamespace

from app.core.telegram_utils import (
    format_manager_notification,
    format_manager_update_notification,
)


def _fake_lead(**kwargs):
    defaults = {
        "car": {"brand": "BMW", "model": "X5", "year": "", "generation": ""},
        "budget": "до 20 000 USD",
        "timeline": "1-3 месяца",
        "experience": "Европа",
        "contact": "+375 (29) 101-52-87",
        "username": "test_user",
        "chat_id": "12345",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class ManagerNotificationTests(unittest.TestCase):
    def test_new_notification_title(self):
        text = format_manager_notification(_fake_lead())
        self.assertIn("НОВАЯ ЗАЯВКА!", text)
        self.assertIn("BMW X5", text)
        self.assertNotIn("ЗАЯВКА ИЗМЕНЕНА", text)

    def test_update_notification_title(self):
        text = format_manager_update_notification(_fake_lead())
        self.assertIn("🔄 ЗАЯВКА ИЗМЕНЕНА", text)
        self.assertIn("Обновлённые данные клиента:", text)
        self.assertIn("BMW X5", text)
        self.assertNotIn("НОВАЯ ЗАЯВКА!", text)

    def test_manager_contact_compact_phone(self):
        text = format_manager_notification(_fake_lead())
        self.assertIn("Контакт: +375291015287", text)
        self.assertNotIn("(29)", text)

    def test_manager_username_display(self):
        text = format_manager_notification(_fake_lead(username="Andry1258"))
        self.assertIn("Username: @Andry1258", text)

        text_unknown = format_manager_notification(_fake_lead(username="unknown"))
        self.assertIn("Username: Не указано", text_unknown)
        self.assertNotIn("@unknown", text_unknown)


if __name__ == "__main__":
    unittest.main()
