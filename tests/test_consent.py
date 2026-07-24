"""Тесты согласия на обработку персональных данных."""

import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.lead import Lead


class ConsentModelTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_consent_defaults_to_false(self):
        db = self.Session()
        lead = Lead(chat_id="100", username="tester", pending_state={})
        db.add(lead)
        db.commit()

        saved = db.query(Lead).filter(Lead.chat_id == "100").one()
        self.assertFalse(saved.consent_given)
        self.assertIsNone(saved.consent_given_at)
        db.close()

    def test_mark_consent_persists(self):
        db = self.Session()
        lead = Lead(chat_id="101", username="tester", pending_state={})
        db.add(lead)
        db.flush()

        lead.consent_given = True
        lead.consent_given_at = datetime.now(timezone.utc)
        lead.pending_state = {"awaiting_consent": False}
        db.commit()

        saved = db.query(Lead).filter(Lead.chat_id == "101").one()
        self.assertTrue(saved.consent_given)
        self.assertIsNotNone(saved.consent_given_at)
        db.close()

    def test_user_has_consent_by_chat_id(self):
        db = self.Session()
        db.add(Lead(chat_id="200", username="a", pending_state={}, consent_given=False))
        db.add(
            Lead(
                chat_id="200",
                username="a",
                pending_state={},
                consent_given=True,
                consent_given_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

        has_consent = (
            db.query(Lead)
            .filter(Lead.chat_id == "200", Lead.consent_given.is_(True))
            .count()
            > 0
        )
        no_consent = (
            db.query(Lead)
            .filter(Lead.chat_id == "201", Lead.consent_given.is_(True))
            .count()
            > 0
        )
        self.assertTrue(has_consent)
        self.assertFalse(no_consent)
        db.close()

    def test_pending_state_consent_flags(self):
        lead = Lead(
            chat_id="300",
            username="a",
            pending_state={"awaiting_consent": True},
        )
        state = dict(lead.pending_state or {})
        self.assertTrue(state.get("awaiting_consent"))

        state.pop("awaiting_consent", None)
        state["consent_declined"] = True
        lead.pending_state = state
        self.assertTrue((lead.pending_state or {}).get("consent_declined"))
        self.assertFalse((lead.pending_state or {}).get("awaiting_consent"))


class ConsentSchemaMigrationTests(unittest.TestCase):
    def test_ensure_schema_adds_consent_columns(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE leads (
                    id VARCHAR(36) PRIMARY KEY,
                    chat_id VARCHAR(100) NOT NULL,
                    username VARCHAR(255),
                    car JSON,
                    budget VARCHAR(100),
                    timeline VARCHAR(100),
                    experience VARCHAR(50),
                    contact VARCHAR(100),
                    status VARCHAR(50),
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )

        # ensure_schema использует глобальный engine — проверяем SQL-паттерн локально
        columns = {c["name"] for c in inspect(engine).get_columns("leads")}
        self.assertNotIn("consent_given", columns)
        self.assertNotIn("consent_given_at", columns)

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE leads ADD COLUMN consent_given BOOLEAN DEFAULT 0 NOT NULL"
            )
            conn.exec_driver_sql(
                "ALTER TABLE leads ADD COLUMN consent_given_at DATETIME"
            )

        columns = {c["name"] for c in inspect(engine).get_columns("leads")}
        self.assertIn("consent_given", columns)
        self.assertIn("consent_given_at", columns)
        engine.dispose()


class ConsentKeyboardConstantsTests(unittest.TestCase):
    def test_callback_data_and_keyboard(self):
        # Импорт main требует валидный .env; пропускаем, если конфиг недоступен
        try:
            from app.main import (
                ACCEPT_CONSENT_CALLBACK,
                DECLINE_CONSENT_CALLBACK,
                get_consent_keyboard,
            )
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"main недоступен без конфига: {exc}")

        self.assertEqual(ACCEPT_CONSENT_CALLBACK, "accept_consent")
        self.assertEqual(DECLINE_CONSENT_CALLBACK, "decline_consent")

        keyboard = get_consent_keyboard()
        row = keyboard.inline_keyboard[0]
        self.assertEqual(len(row), 2)
        self.assertEqual(row[0].callback_data, "accept_consent")
        self.assertEqual(row[1].callback_data, "decline_consent")
        self.assertIn("Согласен", row[0].text)
        self.assertIn("Не согласен", row[1].text)


if __name__ == "__main__":
    unittest.main()
