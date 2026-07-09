import re
from typing import Optional
from app.models.lead import Lead

class UpdateService:
    """Сервис для обработки уточнений и исправлений от клиента"""
    
    # === СПИСОК ПРИВЕТСТВИЙ ===
    GREETING_WORDS = ["привет", "добрый день", "добрый вечер", "здравствуйте", "здравствуй", "доброе утро"]

    # Ключевые слова для каждого поля
    KEYWORDS = {
        "car": ["авто", "машина", "модель", "марка", "выбрал", "передумал авто", "хочу авто", "хочу машину"],
        "budget": ["бюджет", "цена", "стоимость", "денег", "тысяч", "миллион", "передумал бюджет"],
        "timeline": ["срок", "когда", "месяц", "недел", "день", "передумал срок"],
        "experience": ["опыт", "ввоз", "заказывал", "покупал", "передумал опыт"],
        "contact": ["телефон", "номер", "контакт", "позвони", "передумал контакт"],
    }
    
    @staticmethod
    def detect_update(text: str, lead: Lead) -> Optional[tuple]:
        """Проверяет, не пытается ли клиент уточнить/исправить какое-то поле."""
        text_lower = text.lower()
        
        # === ПРОВЕРКА НА ПРИВЕТСТВИЕ ===
        if any(word in text_lower for word in UpdateService.GREETING_WORDS):
            return None
        # =================================
        
        # === ПРОВЕРКА НА ОБЩИЙ ЗАПРОС ОБ ИЗМЕНЕНИИ ===
        general_edit_words = ["хочу изменить", "хочу поменять", "обновить данные", "изменить данные", "поменять данные"]
        if any(word in text_lower for word in general_edit_words):
            return "edit_request", "show_menu"
        # =========================================
        
        for field, keywords in UpdateService.KEYWORDS.items():
            if any(keyword in text_lower for keyword in keywords):
                new_value = UpdateService._extract_value(text, field)
                if new_value:
                    return field, new_value
        return None
    
    @staticmethod
    def _extract_value(text: str, field: str) -> Optional[str]:
        """
        Извлекает новое значение для указанного поля
        """
        text_clean = text.strip()
        
        if field == "car":
            car_match = re.search(r'(?:хочу|выбрал|машина|авто)\s+([\w\s\-]+)', text_clean, re.IGNORECASE)
            if car_match:
                return car_match.group(1).strip()
                
        elif field == "budget":
            numbers = re.findall(r'\d+[\s\.,]?\d*', text_clean)
            if numbers:
                return numbers[0].replace(' ', '') + " тысяч долларов"
                
        elif field == "timeline":
            time_words = ["месяц", "недел", "день", "год", "срочн", "быстр", "скоро"]
            for word in time_words:
                if word in text_clean.lower():
                    return text_clean.strip()
            return None
            
        elif field == "experience":
            if "да" in text_clean.lower() or "есть" in text_clean.lower():
                return "Да, есть опыт"
            elif "нет" in text_clean.lower() or "не" in text_clean.lower():
                return "Нет, первый раз"
            elif "консультация" in text_clean.lower():
                return None
            return None
            
        elif field == "contact":
            phone_match = re.search(r'\+?\d[\d\s\-\(\)]{8,15}', text_clean)
            if phone_match:
                return phone_match.group().strip()
            username_match = re.search(r'@\w+', text_clean)
            if username_match:
                return username_match.group().strip()
            return None
            
        return None