import os
import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'bot.db')}"

# Создаем подключение к базе данных (встроенный драйвер sqlite3 / pysqlite)
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()

# Фабрика сессий (для выполнения запросов)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Базовый класс для всех моделей (таблиц)
Base = declarative_base()

# Функция для получения сессии БД (будет использоваться в запросах)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema() -> None:
    """Добавляет новые колонки в существующую SQLite-таблицу leads."""
    inspector = inspect(engine)
    if "leads" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("leads")}
    migrations = []

    if "pending_state" not in columns:
        migrations.append("ALTER TABLE leads ADD COLUMN pending_state JSON")
    if "export_status" not in columns:
        migrations.append("ALTER TABLE leads ADD COLUMN export_status VARCHAR(50) DEFAULT ''")

    indexes = {idx["name"] for idx in inspector.get_indexes("leads")}
    index_migrations = []
    if "idx_chat_id_status" not in indexes:
        index_migrations.append(
            "CREATE INDEX IF NOT EXISTS idx_chat_id_status ON leads (chat_id, status)"
        )

    if not migrations and not index_migrations:
        return

    with engine.begin() as conn:
        for sql in migrations:
            conn.execute(text(sql))
            logger.info("Применена миграция: %s", sql)
        for sql in index_migrations:
            conn.execute(text(sql))
            logger.info("Применён индекс: %s", sql)