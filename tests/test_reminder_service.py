"""Тесты форматирования напоминаний."""

import unittest

from app.services.reminder_service import (
    REMINDER_POSTPONE_HINT,
    REMINDER_WAITING_PREFIX,
    format_reminder_message,
    normalize_reminder_question,
)


class ReminderMessageTests(unittest.TestCase):
    def test_format_reminder_includes_waiting_phrase(self):
        question = "🚗 Какой автомобиль Вас интересует? (Марка, модель, год)"
        message = format_reminder_message(question)

        self.assertIn(REMINDER_WAITING_PREFIX, message)
        self.assertIn(question, message)
        self.assertIn(REMINDER_POSTPONE_HINT, message)
        self.assertNotIn("Здравствуйте", message)

    def test_normalize_reminder_strips_welcome(self):
        stored = (
            "Здравствуйте! Я AI-помощник компании AutoGroup!\n\n"
            "🚗 Какой автомобиль Вас интересует? (Марка, модель, год)"
        )
        normalized = normalize_reminder_question(stored)
        self.assertEqual(
            normalized,
            "🚗 Какой автомобиль Вас интересует? (Марка, модель, год)",
        )

    def test_normalize_reminder_strips_new_application_prefix(self):
        stored = (
            "Конечно! Давайте создадим новую заявку.\n\n"
            "🚗 Какой автомобиль Вас интересует? (Марка, модель, год)"
        )
        normalized = normalize_reminder_question(stored)
        self.assertEqual(
            normalized,
            "🚗 Какой автомобиль Вас интересует? (Марка, модель, год)",
        )


if __name__ == "__main__":
    unittest.main()
