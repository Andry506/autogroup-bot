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


if __name__ == "__main__":
    unittest.main()
