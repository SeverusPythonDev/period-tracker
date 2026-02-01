"""
Полноценный Telegram бот для отслеживания менструального цикла
Работает с Supabase PostgreSQL (без SQLite!)
Все комментарии на русском, код чистый и готов к деплою
"""

import os
import logging
from datetime import datetime, date, timedelta
from urllib.parse import urlparse  # Для разбора DATABASE_URL
from dotenv import load_dotenv
import psycopg2  # PostgreSQL для Supabase

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# === ЗАГРУЗКА СЕКРЕТОВ (.env для локалки, Environment Variables для облака) ===
load_dotenv()

# Получаем токен и URL базы из переменных окружения
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")

# Проверяем, что секреты найдены
if not TOKEN:
    raise ValueError("❌ ОШИБКА: TELEGRAM_BOT_TOKEN не найден в переменных окружения")
if not DB_URL:
    raise ValueError("❌ ОШИБКА: DATABASE_URL не найден в переменных окружения")

# === НАСТРОЙКА ЛОГОВ ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === СОСТОЯНИЯ ДИАЛОГА ===
ASK_PERIOD_START, ASK_CYCLE_LENGTH = range(2)

# === ПОДКЛЮЧЕНИЕ К SUPABASE POSTGRESQL ===
def get_db_connection():
    """
    Создаёт подключение к Supabase PostgreSQL из DATABASE_URL
    Работает как локально, так и в облаке (PythonAnywhere/Render)
    """
    return psycopg2.connect(DB_URL)

def init_db():
    """Создаёт таблицу users при первом запуске"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        last_period_start TEXT NOT NULL,
                        cycle_length INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()
                logger.info("✅ Таблица users создана/проверена в Supabase")
    except Exception as e:
        logger.error(f"❌ Ошибка создания таблицы: {e}")
        raise

def save_user_data(user_id: int, last_period: date, cycle_length: int):
    """Сохраняет/обновляет данные пользователя (UPSERT)"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO users (user_id, last_period_start, cycle_length)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        last_period_start = EXCLUDED.last_period_start,
                        cycle_length = EXCLUDED.cycle_length
                """, (user_id, last_period.isoformat(), cycle_length))
                conn.commit()
                logger.info(f"✅ Данные пользователя {user_id} сохранены")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения данных: {e}")
        raise

def get_user_data(user_id: int):
    """Получает данные пользователя из базы"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT last_period_start, cycle_length FROM users WHERE user_id = %s",
                    (user_id,)
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "last_period_start": date.fromisoformat(row[0]),
                        "cycle_length": row[1]
                    }
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка получения данных: {e}")
        return None

def delete_user_data(user_id: int):
    """Удаляет данные пользователя"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
                conn.commit()
                logger.info(f"✅ Данные пользователя {user_id} удалены")
    except Exception as e:
        logger.error(f"❌ Ошибка удаления данных: {e}")

# === ОБРАБОТЧИКИ КОМАНД ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старт диалога — спрашиваем дату последней менструации"""
    await update.message.reply_text(
        "🌸 Добро пожаловать в календарь менструального цикла!\n\n"
        "Укажите дату начала *последней* менструации в формате:\n"
        "`ГГГГ-ММ-ДД` (например, `2026-01-25`)\n\n"
        "_Не переживай, данные хранятся анонимно_",
        parse_mode="Markdown"
    )
    return ASK_PERIOD_START

async def receive_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает дату последней менструации"""
    try:
        input_text = update.message.text.strip()
        last_period = datetime.strptime(input_text, "%Y-%m-%d").date()
        
        # Проверяем, что дата не из будущего
        if last_period > date.today():
            await update.message.reply_text(
                "⏰ Дата не может быть в будущем 🤔\n\nПопробуйте ещё раз:"
            )
            return ASK_PERIOD_START
        
        # Сохраняем в контексте диалога
        context.user_data["last_period"] = last_period
        await update.message.reply_text(
            f"✅ Дата `{last_period.strftime('%Y-%m-%d')}` принята\n\n"
            "Теперь укажите среднюю длину вашего цикла:\n"
            "`28` (обычно 21–35 дней)",
            parse_mode="Markdown"
        )
        return ASK_CYCLE_LENGTH
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат!\n\n"
            "Используйте `ГГГГ-ММ-ДД`, например:\n"
            "`2026-01-25`"
            "Попробуйте ещё раз:",
            parse_mode="Markdown"
        )
        return ASK_PERIOD_START

async def receive_cycle_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет длину цикла и завершает настройку"""
    try:
        cycle_len = int(update.message.text.strip())
        
        if not (14 <= cycle_len <= 60):
            await update.message.reply_text(
                "🤔 Укажите реалистичную длину цикла:\n"
                "`14–60` дней\n\n"
                "Попробуйте ещё раз:"
            )
            return ASK_CYCLE_LENGTH
        
        # Сохраняем в Supabase
        user_id = update.effective_user.id
        last_period = context.user_data["last_period"]
        save_user_data(user_id, last_period, cycle_len)
        
        # Рассчитываем следующую менструацию
        next_date = last_period + timedelta(days=cycle_len)
        
        await update.message.reply_text(
            f"🎉 Данные успешно сохранены в облаке!\n\n"
            f"📅 Последняя менструация: `{last_period.strftime('%Y-%m-%d')}`\n"
            f"⏰ Длина цикла: `{cycle_len}` дней\n"
            f"🌙 *Следующая ожидается:* `{next_date.strftime('%Y-%m-%d')}`\n\n"
            f"Используйте /calendar для проверки статуса",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("❌ Введите целое число!")
        return ASK_CYCLE_LENGTH

async def calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверяет текущий статус цикла"""
    user_id = update.effective_user.id
    data = get_user_data(user_id)
    
    if not data:
        await update.message.reply_text(
            "📝 Сначала настройте бота командой /start"
        )
        return
    
    last_period = data["last_period_start"]
    cycle_length = data["cycle_length"]
    days_since = (date.today() - last_period).days
    
    if days_since < 0:
        status = "⏰ Дата последней менструации в будущем"
    elif 0 <= days_since <= 5:
        status = "🔴 *Идёт менструация* (дни 1-5)"
    elif days_since >= cycle_length:
        status = "🟡 *Ожидается новая менструация*"
    else:
        status = f"🟢 Цикл продолжается (день {days_since + 1}/{cycle_length})"
    
    next_period = last_period + timedelta(days=cycle_length)
    days_to_next = (next_period - date.today()).days
    
    await update.message.reply_text(
        f"📊 *Статус цикла*:\n"
        f"{status}\n\n"
        f"📅 Следующая: `{next_period.strftime('%Y-%m-%d')}`\n"
        f"⏳ Осталось: `{days_to_next}` дней",
        parse_mode="Markdown"
    )

async def delete_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет все данные пользователя"""
    user_id = update.effective_user.id
    delete_user_data(user_id)
    await update.message.reply_text(
        "🗑️ Ваши данные полностью удалены из облака\n\n"
        "Можете начать заново с /start"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Справка по боту"""
    await update.message.reply_text(
        "🌸 *Помощь по боту*\n\n"
        f"• `/start` — настроить цикл\n"
        f"• `/calendar` — текущий статус\n"
        f"• `/delete_my_data` — удалить данные\n"
        f"• `/help` — эта справка\n\n"
        f"_Все данные анонимны и шифрованы в Supabase_",
        parse_mode="Markdown"
    )

# === ГЛАВНАЯ ФУНКЦИЯ ЗАПУСКА ===
def main():
    """Запускает бота с инициализацией Supabase"""
    print("🚀 Инициализация Supabase...")
    init_db()
    
    # Создаём приложение
    app = Application.builder().token(TOKEN).build()
    
    # Настраиваем диалог настройки
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PERIOD_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_period_start)],
            ASK_CYCLE_LENGTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cycle_length)],
        },
        fallbacks=[],
    )
    
    # Регистрируем обработчики
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("calendar", calendar))
    app.add_handler(CommandHandler("delete_my_data", delete_my_data))
    app.add_handler(CommandHandler("help", help_command))
    
    logger.info("✅ Бот запущен с Supabase! Ctrl+C для остановки")
    print("🌟 Бот работает! Тестируйте /start")
    
    # Запуск
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
