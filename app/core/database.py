from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "sqlite:////data/bot.db"

# Создаем подключение к базе данных (встроенный драйвер sqlite3 / pysqlite)
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

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