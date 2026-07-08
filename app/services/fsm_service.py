from enum import Enum
from typing import Optional

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
        LeadField.BUDGET: "💰 Какой бюджет Вы рассматриваете? (в USD, EUR или BYN)",
        LeadField.TIMELINE: "📅 Когда планируете покупку? (1-3 мес, 3-6 мес, >6 мес)",
        LeadField.EXPERIENCE: "📦 У Вас есть опыт ввоза авто из Китая? (да/нет/первый раз)",
        LeadField.CONTACT: "📱 Оставьте телефон или @telegram для связи"
    }
    
    @staticmethod
    def get_next_field(lead_data: dict) -> Optional[LeadField]:
        """
        Определяет, какое поле нужно запросить следующим.
        Возвращает None, если все поля заполнены.
        """
        for field in LeadField:
            # Если поле пустое или отсутствует — запрашиваем его
            if not lead_data.get(field.value, ""):
                return field
        return None  # Все поля заполнены
    
    @staticmethod
    def get_question_for_field(field: LeadField) -> str:
        """Возвращает вопрос для указанного поля"""
        return FSMService.QUESTIONS.get(field, "Расскажите подробнее")
    
    @staticmethod
    def is_completed(lead_data: dict) -> bool:
        """Проверяет, заполнены ли все поля"""
        return all(lead_data.get(field.value, "") for field in LeadField)
    
    @staticmethod
    def needs_clarification(text: str, field: LeadField) -> bool:
        """
        Проверяет, нужно ли показать варианты ответа.
        """
        # Простые проверки для каждого поля
        if field == LeadField.BUDGET:
            # Если нет цифр — нужны варианты
            if not any(char.isdigit() for char in text):
                return True
            # Если есть слова "сколько", "столько", "нормально" — нужны варианты
            vague_words = ["сколько", "столько", "нормально", "хорошо", "норм"]
            if any(word in text.lower() for word in vague_words):
                return True
                
        if field == LeadField.TIMELINE:
            # Если нет указания на время — нужны варианты
            time_words = ["месяц", "день", "недел", "год", "срочн", "быстр", "скоро"]
            if not any(word in text.lower() for word in time_words):
                return True
                
        if field == LeadField.EXPERIENCE:
            # Если не "да", "нет", "первый" — нужны варианты
            experience_words = ["да", "нет", "первый", "есть", "не", "опыт"]
            if not any(word in text.lower() for word in experience_words):
                return True
                
        return False