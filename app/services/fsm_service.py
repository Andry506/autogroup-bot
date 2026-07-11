from enum import Enum
from typing import Optional

from app.core.options import BUDGET_OPTIONS, MARKET_OPTIONS, TIMELINE_OPTIONS


class LeadField(Enum):
    """Список полей, которые нужно собрать у клиента (по порядку)"""
    CAR = "car"
    BUDGET = "budget"
    TIMELINE = "timeline"
    EXPERIENCE = "experience"
    CONTACT = "contact"


LEAD_FIELDS = [field.value for field in LeadField]

class FSMService:
    """
    Сервис управления вопросами к клиенту.
    Все вопросы жестко закодированы — никакого LLM здесь!
    """
    
    # Вопросы для каждого поля (порядок важен!)
    QUESTIONS = {
        LeadField.CAR: "🚗 Какой автомобиль Вас интересует? (Марка, модель, год)",
        LeadField.BUDGET: "💰 Какой бюджет Вы рассматриваете? Выберите вариант:",
        LeadField.TIMELINE: "📅 Когда планируете покупку? (1-3 мес, 3-6 мес, >6 мес)",
        LeadField.EXPERIENCE: "🌍 Какой рынок (США, Европа, Корея или Китай) рассматриваете к покупке?",
        LeadField.CONTACT: "📱 Оставьте телефон или @telegram для связи"
    }
    
    @staticmethod
    def get_next_field(lead_data: dict) -> Optional[LeadField]:
        """
        Определяет, какое поле нужно запросить следующим.
        Возвращает None, если все поля заполнены.
        """
        for field in LeadField:
            if not lead_data.get(field.value, ""):
                return field
        return None
    
    @staticmethod
    def get_question_for_field(field: LeadField) -> str:
        """Возвращает вопрос для указанного поля"""
        return FSMService.QUESTIONS.get(field, "Расскажите подробнее")
    
    @staticmethod
    def is_completed(lead_data: dict) -> bool:
        """Проверяет, заполнены ли все поля"""
        return all(lead_data.get(field.value, "") for field in LeadField)
