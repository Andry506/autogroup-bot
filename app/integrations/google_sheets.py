import gspread
from google.oauth2.service_account import Credentials
import os
import json
from datetime import datetime, timezone, timedelta
from app.core.config import config

class GoogleSheetsClient:
    """Клиент для работы с Google Sheets"""

    SHEET_HEADERS = [
        "Дата",
        "Время",
        "Chat ID",
        "Username",
        "Авто",
        "Бюджет",
        "Срок",
        "Опыт",
        "Контакт",
        "Статус",
    ]
    
    def __init__(self):
        self.credentials_file = config.GOOGLE_CREDENTIALS_FILE
        self.sheet_id = config.GOOGLE_SHEET_ID
        self.sheet_name = getattr(config, "GOOGLE_SHEET_NAME", "AI Lead Agent - Заявки")
        self.client = None
        self.sheet = None
        
        # === ДИАГНОСТИКА ===
        print(f"🔍 credentials_file: {self.credentials_file}")
        print(f"🔍 sheet_id из config: {self.sheet_id}")
        print(f"🔍 Файл существует: {os.path.exists(self.credentials_file)}")
        print(f"🔍 Текущая директория: {os.getcwd()}")
        # ===================
        
        # Проверяем наличие ID таблицы
        if not self.sheet_id:
            print("⚠️ GOOGLE_SHEET_ID не задан в .env. Google Sheets не будет работать.")
            return
        
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            creds = None
            
            # === ГЛАВНОЕ: ПЫТАЕМСЯ ПОЛУЧИТЬ CREDENTIALS ИЗ ПЕРЕМЕННОЙ ===
            if config.GOOGLE_CREDENTIALS_JSON:
                try:
                    creds_dict = json.loads(config.GOOGLE_CREDENTIALS_JSON)
                    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
                    print("✅ Google Sheets подключён через переменную окружения")
                except Exception as e:
                    print(f"❌ Ошибка парсинга GOOGLE_CREDENTIALS_JSON: {e}")
            
            # === ЗАПАСНОЙ ВАРИАНТ: ПЫТАЕМСЯ ИЗ ФАЙЛА ===
            if not creds and os.path.exists(self.credentials_file):
                try:
                    creds = Credentials.from_service_account_file(self.credentials_file, scopes=scopes)
                    print(f"✅ Google Sheets подключён через файл {self.credentials_file}")
                except Exception as e:
                    print(f"❌ Ошибка загрузки credentials из файла: {e}")
            
            if not creds:
                print("⚠️ Не найдены credentials ни в переменной GOOGLE_CREDENTIALS_JSON, ни в файле")
                return
            
            # Авторизация
            self.client = gspread.authorize(creds)
            
            # Открываем таблицу по ID
            try:
                self.sheet = self.client.open_by_key(self.sheet_id).sheet1
                print(f"✅ Google Sheets подключён по ID: {self.sheet_id} (лист: {self.sheet.title})")
            except gspread.exceptions.SpreadsheetNotFound:
                print(f"❌ Таблица с ID {self.sheet_id} не найдена.")
                print("📌 Проверьте доступ: добавьте email сервисного аккаунта в таблицу")
                if creds.service_account_email:
                    print(f"📧 Email сервисного аккаунта: {creds.service_account_email}")
            except Exception as e:
                print(f"❌ Ошибка открытия таблицы: {e}")
                
        except Exception as e:
            print(f"❌ Ошибка подключения к Google Sheets: {e}")
    
    def _setup_headers(self):
        """Создает заголовки для таблицы (если нужно создать новую)"""
        try:
            self.sheet.insert_row(self.SHEET_HEADERS, index=1)
            print("✅ Заголовки таблицы созданы")
        except Exception as e:
            print(f"❌ Ошибка создания заголовков: {e}")
    
    def add_lead(self, lead_data: dict):
        """Добавляет заявку в таблицу"""
        if not self.sheet:
            print(
                f"⚠️ Google Sheets недоступен. Заявка chat_id={lead_data.get('chat_id')} не сохранена."
            )
            return False
        
        try:
            # Устанавливаем часовой пояс Минск (UTC+3)
            tz = timezone(timedelta(hours=3))
            now = datetime.now(tz)
            
            row = [
                now.strftime("%d.%m.%Y"),
                now.strftime("%H:%M"),
                lead_data.get("chat_id", ""),
                lead_data.get("username", ""),
                lead_data.get("car", ""),
                lead_data.get("budget", ""),
                lead_data.get("timeline", ""),
                lead_data.get("experience", ""),
                lead_data.get("contact", ""),
                lead_data.get("status", "Новая"),
            ]
            
            self.sheet.append_row(row)
            print(
                f"✅ Заявка chat_id={lead_data.get('chat_id')} сохранена в Google Sheets"
            )
            return True
        except Exception as e:
            print(f"❌ Ошибка сохранения в Google Sheets: {e}")
            return False
    
    def get_all_leads(self):
        """Получить все заявки из таблицы"""
        if not self.sheet:
            return []
        
        try:
            return self.sheet.get_all_records()
        except Exception as e:
            print(f"❌ Ошибка получения данных: {e}")
            return []