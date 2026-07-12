"""Тесты валидации контакта."""

import unittest

from app.core.contact_validation import is_valid_contact


class ContactValidationTests(unittest.TestCase):
    def test_valid_phones(self):
        self.assertTrue(is_valid_contact("+375291234567"))
        self.assertTrue(is_valid_contact("8 (029) 123-45-67"))
        self.assertTrue(is_valid_contact("1234567"))

    def test_valid_username(self):
        self.assertTrue(is_valid_contact("@username"))

    def test_invalid_phones(self):
        self.assertFalse(is_valid_contact("123456"))
        self.assertFalse(is_valid_contact("1234567890123456"))
        self.assertFalse(is_valid_contact("abc123"))


if __name__ == "__main__":
    unittest.main()
