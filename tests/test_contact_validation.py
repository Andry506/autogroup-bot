"""Тесты валидации контакта."""

import unittest

from app.core.contact_validation import (
    count_phone_digits,
    format_phone_number,
    is_phone_number_valid,
    is_valid_contact,
    normalize_contact,
    INVALID_COUNTRY_CODE_MESSAGE,
)


class ContactValidationTests(unittest.TestCase):
    def test_valid_phone_numbers(self):
        self.assertTrue(is_valid_contact("+375291234567"))
        self.assertTrue(is_valid_contact("+375 (29) 123-45-67"))
        self.assertTrue(is_valid_contact("+79161234567"))

    def test_valid_username(self):
        self.assertTrue(is_valid_contact("@username"))

    def test_invalid_phone_numbers(self):
        self.assertFalse(is_valid_contact("123456"))
        self.assertFalse(is_valid_contact("1234567890123456"))
        self.assertFalse(is_valid_contact("phone 123"))
        self.assertFalse(is_valid_contact("abc1234567"))
        self.assertFalse(is_valid_contact("+9991234567890"))

    def test_count_phone_digits(self):
        self.assertEqual(count_phone_digits("+375 (29) 123-45-67"), 12)

    def test_format_belarus_phone(self):
        self.assertEqual(format_phone_number("+375291015287"), "+375 (29) 101-52-87")

    def test_normalize_contact(self):
        ok, formatted, error = normalize_contact("+375291015287")
        self.assertTrue(ok)
        self.assertEqual(formatted, "+375 (29) 101-52-87")
        self.assertEqual(error, "")

    def test_invalid_country_code_message(self):
        ok, _, error = normalize_contact("+9991234567890")
        self.assertFalse(ok)
        self.assertEqual(error, INVALID_COUNTRY_CODE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
