from sqlalchemy import Column, String, DateTime, JSON, Index, Integer
from app.core.database import Base
from datetime import datetime
import uuid

class Lead(Base):
    """Модель заявки клиента"""
    __tablename__ = "leads"

    __table_args__ = (
        Index("idx_chat_id_status", "chat_id", "status"),
    )
    
    # Уникальный ID заявки
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    
    # Информация о клиенте
    chat_id = Column(String(100), nullable=False, index=True)  # ID в Telegram
    username = Column(String(255))  # Username в Telegram
    
    # Собираемые поля (заполняются постепенно)
    car = Column(JSON, default=lambda: {})  # {brand, model, year, generation}
    budget = Column(String(100), default="")  # Бюджет
    timeline = Column(String(100), default="")  # Срок покупки
    experience = Column(String(50), default="")  # Опыт ввоза
    contact = Column(String(100), default="")  # Телефон или @telegram
    
    # Метаданные
    status = Column(String(50), default="collecting")  # collecting, completed, transferred
    export_status = Column(String(50), default="")  # pending, exported, failed
    pending_state = Column(JSON, default=dict)  # Промежуточное состояние диалога
    dialog_history = Column(JSON, default=list)  # История всего диалога
    # message_id исходного уведомления менеджеру (для reply при изменениях)
    manager_notification_message_id = Column(Integer, nullable=True)
    
    # Даты
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)