from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.core.config import config

# Создаем подключение к базе данных
# Для SQLite нужно добавить check_same_thread=False
engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in config.DATABASE_URL else {}
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