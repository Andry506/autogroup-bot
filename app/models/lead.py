from sqlalchemy import Column, String, DateTime, JSON
from app.core.database import Base
from datetime import datetime
import uuid

class Lead(Base):
    """Модель заявки клиента"""
    __tablename__ = "leads"
    
    # Уникальный ID заявки
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    
    # Информация о клиенте
    chat_id = Column(String(100), nullable=False, index=True)  # ID в Telegram
    username = Column(String(255))  # Username в Telegram
    
    # Собираемые поля (заполняются постепенно)
    car = Column(String(255), default="")  # Марка/модель авто
    budget = Column(String(100), default="")  # Бюджет
    timeline = Column(String(100), default="")  # Срок покупки
    experience = Column(String(50), default="")  # Опыт ввоза
    contact = Column(String(100), default="")  # Телефон или @telegram
    
    # Метаданные
    status = Column(String(50), default="collecting")  # collecting, completed, transferred
    export_status = Column(String(50), default="")  # pending, exported, failed
    pending_state = Column(JSON, default=dict)  # Промежуточное состояние диалога
    dialog_history = Column(JSON, default=list)  # История всего диалога
    
    # Даты
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)