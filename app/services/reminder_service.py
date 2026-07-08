import asyncio
import logging
from typing import Dict, Optional
from aiogram import Bot

logger = logging.getLogger(__name__)

class ReminderService:
    """
    Сервис для отправки напоминаний клиентам.
    Использует asyncio для планирования задач (работает в Webhook-режиме).
    """

    def __init__(self, bot: Bot):
        self.bot = bot
        self._tasks: Dict[str, asyncio.Task] = {}
        self._reminder_count: Dict[str, int] = {}
        self._running = True
        logger.info("✅ ReminderService инициализирован (asyncio)")

    def start(self):
        """Запускает сервис (не нужен для asyncio)"""
        logger.info("🚀 ReminderService готов к работе")

    def schedule_reminder(
        self,
        chat_id: str,
        question_text: str,
        delay_seconds: int = 300,
        max_reminders: int = 2,
        interval_seconds: int = 300
    ):
        """
        Планирует отправку напоминания клиенту.
        """
        # Отменяем предыдущие напоминания для этого клиента
        self.cancel_reminder(chat_id)

        # Сбрасываем счетчик напоминаний
        self._reminder_count[chat_id] = 0

        # Создаем задачу
        task = asyncio.create_task(
            self._remind_loop(
                chat_id=chat_id,
                question_text=question_text,
                delay_seconds=delay_seconds,
                max_reminders=max_reminders,
                interval_seconds=interval_seconds
            )
        )
        self._tasks[chat_id] = task
        logger.info(f"⏰ Напоминание запланировано для chat_id={chat_id} через {delay_seconds} сек")

    async def _remind_loop(
        self,
        chat_id: str,
        question_text: str,
        delay_seconds: int,
        max_reminders: int,
        interval_seconds: int
    ):
        """Цикл отправки напоминаний"""
        try:
            # Ждем первую задержку
            await asyncio.sleep(delay_seconds)

            # Проверяем, не отменена ли задача
            if chat_id not in self._tasks:
                return

            reminders_sent = 0
            while reminders_sent < max_reminders and chat_id in self._tasks:
                try:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🤔 Я все еще жду ваш ответ: {question_text}\n\n"
                            f"Если вам нужно больше времени — просто напишите 'позже'."
                        )
                    )
                    reminders_sent += 1
                    self._reminder_count[chat_id] = reminders_sent
                    logger.info(f"📨 Напоминание #{reminders_sent} отправлено для chat_id={chat_id}")
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки напоминания для chat_id={chat_id}: {e}")
                    break

                # Если это последнее напоминание — выходим
                if reminders_sent >= max_reminders:
                    break

                # Ждем интервал перед следующим напоминанием
                await asyncio.sleep(interval_seconds)

            # Удаляем задачу после завершения
            if chat_id in self._tasks:
                del self._tasks[chat_id]
            if chat_id in self._reminder_count:
                del self._reminder_count[chat_id]
            logger.info(f"⏰ Цикл напоминаний завершен для chat_id={chat_id}")

        except asyncio.CancelledError:
            logger.info(f"⏰ Напоминание для chat_id={chat_id} было отменено")
            if chat_id in self._tasks:
                del self._tasks[chat_id]
            if chat_id in self._reminder_count:
                del self._reminder_count[chat_id]
        except Exception as e:
            logger.error(f"❌ Ошибка в цикле напоминаний для chat_id={chat_id}: {e}")
            if chat_id in self._tasks:
                del self._tasks[chat_id]
            if chat_id in self._reminder_count:
                del self._reminder_count[chat_id]

    def cancel_reminder(self, chat_id: str):
        """
        Отменяет все активные напоминания для клиента.
        """
        if chat_id in self._tasks:
            self._tasks[chat_id].cancel()
            del self._tasks[chat_id]
            logger.info(f"🛑 Напоминание отменено для chat_id={chat_id}")
        
        if chat_id in self._reminder_count:
            del self._reminder_count[chat_id]

    def stop(self):
        """Останавливает все напоминания"""
        self._running = False
        for chat_id, task in list(self._tasks.items()):
            task.cancel()
        self._tasks.clear()
        self._reminder_count.clear()
        logger.info("🛑 ReminderService остановлен")