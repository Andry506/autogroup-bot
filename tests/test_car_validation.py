"""Тесты валидации и нормализации поля car."""

import unittest

from app.core.car_validation import (
    CAR_UNKNOWN_VALUE,
    car_to_db,
    format_car_display,
    is_car_answer_valid,
    is_car_field_valid,
    is_car_unknown_response,
    normalize_car,
    parse_car_fast,
    strip_greetings_from_car_text,
    validate_model_candidate,
)


class CarValidationAcceptTests(unittest.TestCase):
    CASES = [
        ("BMW X5", "BMW", "X5"),
        ("тайота камри", "Toyota", "Camry"),
        ("бмв х5", "BMW", "X5"),
        ("BYD Song", "BYD", "Song"),
        ("хавал джолион", "Haval", "Jolion"),
        ("Geely Galaxy Starship 7", "Geely", "Galaxy Starship 7"),
        ("BYD Sealion 7", "BYD", "Sealion 7"),
        ("тойота равчик", "Toyota", "RAV4"),
        ("geely монжаро", "Geely", "Monjaro"),
        ("Добрый день. Audi RS6", "Audi", "RS6"),
        ("Mersedes S63", "Mercedes", "S63"),
    ]

    def test_parse_car_fast(self):
        for raw, brand, model in self.CASES:
            with self.subTest(raw=raw):
                result = parse_car_fast(raw)
                self.assertEqual(result.get("status"), "ok", f"должно приниматься: {raw!r}")
                self.assertEqual(result.get("brand"), brand)
                self.assertEqual(result.get("model"), model)

    def test_normalize_car_structure(self):
        result = normalize_car("бмв х5")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["brand"], "BMW")
        self.assertEqual(result["model"], "X5")

    def test_format_car_display(self):
        stored = car_to_db({"brand": "BMW", "model": "X5", "year": "2022", "generation": ""})
        self.assertEqual(format_car_display(stored), "BMW X5 2022")


class CarValidationRejectTests(unittest.TestCase):
    CASES = [
        "BMW цветочек",
        "hello",
        "123",
        "Camry",
        "красная машина",
        "хороший автомобиль",
    ]

    def test_reject(self):
        for raw in self.CASES:
            with self.subTest(raw=raw):
                self.assertFalse(is_car_field_valid(raw), f"должно отклоняться: {raw!r}")
                self.assertNotEqual(parse_car_fast(raw).get("status"), "ok")


class CarUnknownTests(unittest.TestCase):
    def test_unknown_phrases(self):
        for phrase in ("не знаю", "пока не уверен", "затрудняюсь ответить", "не могу выбрать"):
            with self.subTest(phrase=phrase):
                self.assertTrue(is_car_unknown_response(phrase))
                self.assertTrue(is_car_field_valid(phrase))

    def test_random_text_not_unknown(self):
        self.assertFalse(is_car_unknown_response("BMW X5 и Audi e-tron"))


class CarMultiTests(unittest.TestCase):
    def test_multi_car_valid(self):
        self.assertTrue(is_car_field_valid("BMW X5 и Audi e-tron"))


class CarUnknownDisplayTests(unittest.TestCase):
    def test_unknown_display(self):
        stored = car_to_db({"brand": "", "model": CAR_UNKNOWN_VALUE, "year": "", "generation": ""})
        self.assertEqual(format_car_display(stored), CAR_UNKNOWN_VALUE)


class ValidateModelCandidateTests(unittest.TestCase):
    def test_allow_flexible_models(self):
        self.assertTrue(validate_model_candidate("Galaxy Starship 7"))
        self.assertTrue(validate_model_candidate("Sealion 7"))
        self.assertTrue(validate_model_candidate("iX"))

    def test_reject_colors_and_noise(self):
        self.assertFalse(validate_model_candidate("красный"))
        self.assertFalse(validate_model_candidate("машина"))


if __name__ == "__main__":
    unittest.main()
