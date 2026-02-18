import os
import logging
from datetime import datetime, date, timedelta
import sqlite3
from aiohttp import web

# === ПЕРЕМЕННЫЕ ===
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN не найден!")

WEBHOOK_HOST = os.getenv("RAILWAY_PUBLIC_URL", "")
if not WEBHOOK_HOST:
    raise ValueError("❌ RAILWAY_PUBLIC_URL не задан")

WEBHOOK_PATH = f"/webhook/{TOKEN}"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

DATABASE_PATH = "/tmp/periods.db"

# === AIORAM ===
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# === ПАРСИНГ ДАТЫ ===
def parse_world_date(text):
    clean = text.strip().replace(" ", "")
    patterns = ["%d.%m.%Y", "%d.%m.%y", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y-%m-%d", "%Y/%m/%d"]
    for fmt in patterns:
        try:
            date_obj = datetime.strptime(clean, fmt)
            if 1 <= date_obj.day <= 31 and 1 <= date_obj.month <= 12:
                return date_obj
        except:
            continue
    return None

# === КЛАВИАТУРЫ ===
main_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📊 Мой цикл"), KeyboardButton(text="📈 Предсказание")],
    [KeyboardButton(text="💊 Симптомы"), KeyboardButton(text="➕ Новый цикл")],
    [KeyboardButton(text="🗑️ Удалить данные"), KeyboardButton(text="🆘 Помощь")]
], resize_keyboard=True)

cycle_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🔴 Менструация"), KeyboardButton(text="🟢 Фолликуляр"), KeyboardButton(text="🟡 Овуляция")],
    [KeyboardButton(text="🟣 Лютеин"), KeyboardButton(text="🏠 Главное меню")]
], resize_keyboard=True)

symptom_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Боли в животе", callback_data="symptom:cramps")],
    [InlineKeyboardButton(text="Чувствительность груди", callback_data="symptom:breast")],
    [InlineKeyboardButton(text="Головная боль", callback_data="symptom:headache")],
    [InlineKeyboardButton(text="Усталость", callback_data="symptom:fatigue")],
    [InlineKeyboardButton(text="Готово ✅", callback_data="symptom_done")]
])

# === FSM ===
class NewCycle(StatesGroup):
    waiting_period_start = State()
    waiting_period_length = State()
    waiting_cycle_length = State()

# === БАЗА ===
def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    return conn

def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id BIGINT NOT NULL,
                period_start TEXT NOT NULL,
                period_length INTEGER NOT NULL,
                cycle_length INTEGER NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS symptoms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id BIGINT NOT NULL,
                cycle_id INTEGER NOT NULL,
                cycle_day INTEGER NOT NULL,
                symptom TEXT NOT NULL,
                FOREIGN KEY (cycle_id) REFERENCES cycles(id)
            )
        """)
        conn.commit()

def get_last_cycle(user_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, period_start, period_length, cycle_length
            FROM cycles
            WHERE user_id = ?
            ORDER BY period_start DESC LIMIT 1
        """, (user_id,))
        return cursor.fetchone()

# === ХЕНДЛЕРЫ ===
@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer("🌸 Добро пожаловать! Нажми ➕ Новый цикл.", reply_markup=main_kb)

@dp.message(F.text == "➕ Новый цикл")
async def new_cycle_start(message: Message, state: FSMContext):
    await message.answer("📅 Дата начала месячных? (15.02.26)", reply_markup=main_kb)
    await state.set_state(NewCycle.waiting_period_start)

@dp.message(NewCycle.waiting_period_start)
async def process_period_start(message: Message, state: FSMContext):
    date_obj = parse_world_date(message.text)
    if not date_obj:
        await message.answer("❌ Не понял дату. Пример: 15.02.26", reply_markup=main_kb)
        return
    await state.update_data(period_start=date_obj.date())
    await message.answer("📏 Сколько дней шли месячные? (1–10)", reply_markup=main_kb)
    await state.set_state(NewCycle.waiting_period_length)

@dp.message(NewCycle.waiting_period_length)
async def process_period_length(message: Message, state: FSMContext):
    try:
        days = int(message.text)
        if not (1 <= days <= 10):
            raise ValueError
        await state.update_data(period_length=days)
        await message.answer("🌀 Длина цикла? (21–40)", reply_markup=main_kb)
        await state.set_state(NewCycle.waiting_cycle_length)
    except:
        await message.answer("❌ Введи число от 1 до 10.", reply_markup=main_kb)

@dp.message(NewCycle.waiting_cycle_length)
async def process_cycle_length(message: Message, state: FSMContext):
    try:
        cycle_len = int(message.text)
        if not (21 <= cycle_len <= 40):
            raise ValueError
        data = await state.get_data()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO cycles (user_id, period_start, period_length, cycle_length)
                VALUES (?, ?, ?, ?)
            """, (message.from_user.id, data["period_start"].isoformat(), data["period_length"], cycle_len))
            conn.commit()
        await message.answer(
            f"✅ Цикл сохранён!\n"
            f"📅 Месячные: {data['period_start'].strftime('%d.%m.%Y')} ({data['period_length']} дн.)\n"
            f"🌀 Цикл: {cycle_len} дней",
            reply_markup=main_kb
        )
        await state.clear()
    except:
        await message.answer("❌ Цикл 21–40 дней.", reply_markup=main_kb)

@dp.message(F.text == "📊 Мой цикл")
async def my_cycle(message: Message):
    user_id = message.from_user.id
    cycle = get_last_cycle(user_id)
    if not cycle:
        await message.answer("📝 Сначала ➕ Новый цикл", reply_markup=main_kb)
        return

    cycle_id, period_start_str, period_length, cycle_length = cycle
    last_period = date.fromisoformat(period_start_str)
    today = date.today()
    days_since = (today - last_period).days
    next_period = last_period + timedelta(days=cycle_length)
    days_to_next = (next_period - today).days

    if days_since < 0:
        status = "⏳ Цикл не начался"
    elif days_since < period_length:
        status = "🔴 Месячные"
    elif days_to_next <= 5 and days_to_next > 0:
        status = "🌫️ ПМС"
    elif days_since < cycle_length // 2:
        status = "🟢 Фолликулярная"
    elif days_since < cycle_length // 2 + 2:
        status = "🟡 Овуляция"
    else:
        status = "🟣 Лютеиновая"

    await message.answer(
        f"📊 СТАТУС\n{status}\n"
        f"📅 Следующие: {next_period.strftime('%d.%m.%Y')}\n"
        f"⏳ До них: {max(0, days_to_next)} дней",
        reply_markup=cycle_kb
    )

@dp.message(F.text == "📈 Предсказание")
async def prediction(message: Message):
    user_id = message.from_user.id
    cycle = get_last_cycle(user_id)
    if not cycle:
        await message.answer("📝 Сначала добавьте цикл через ➕ Новый цикл", reply_markup=main_kb)
        return

    _, period_start_str, _, cycle_length = cycle
    last_period = date.fromisoformat(period_start_str)
    next_period = last_period + timedelta(days=cycle_length)
    ovulation_day = last_period + timedelta(days=cycle_length // 2)

    await message.answer(
        f"🔮 Предсказание:\n"
        f"📅 Следующие месячные: {next_period.strftime('%d.%m.%Y')}\n"
        f"🥚 Овуляция: {ovulation_day.strftime('%d.%m.%Y')}\n"
        f"💡 ПМС начнётся за ~5 дней до месячных",
        reply_markup=main_kb
    )

@dp.message(F.text == "💊 Симптомы")
async def log_symptoms(message: Message):
    user_id = message.from_user.id
    cycle = get_last_cycle(user_id)
    if not cycle:
        await message.answer("Сначала ➕ Новый цикл", reply_markup=main_kb)
        return

    cycle_id, period_start_str, period_length, cycle_length = cycle
    last_period = date.fromisoformat(period_start_str)
    day_of_cycle = (date.today() - last_period).days + 1

    if day_of_cycle < 1 or day_of_cycle > cycle_length:
        await message.answer("Сегодня вне активного цикла. Добавьте новый, если начался!", reply_markup=main_kb)
        return

    await message.answer(f"💊 День {day_of_cycle}: выбери симптомы", reply_markup=symptom_kb)

@dp.callback_query(F.data.startswith("symptom:"))
async def save_symptom(callback: CallbackQuery):
    symptom_key = callback.data.split(":")[1]
    symptom_map = {"cramps": "Боли в животе", "breast": "Чувствительность груди", "headache": "Головная боль", "fatigue": "Усталость"}
    name = symptom_map.get(symptom_key, "Неизвестный")

    user_id = callback.from_user.id
    cycle = get_last_cycle(user_id)
    if not cycle:
        await callback.answer("❌ Нет цикла")
        return

    cycle_id, period_start_str, _, _ = cycle
    last_period = date.fromisoformat(period_start_str)
    day = (date.today() - last_period).days + 1

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO symptoms (user_id, cycle_id, cycle_day, symptom) VALUES (?, ?, ?, ?)", (user_id, cycle_id, day, name))
        conn.commit()
    await callback.answer(f"✅ {name}")

@dp.callback_query(F.data == "symptom_done")
async def done_symptoms(callback: CallbackQuery):
    await callback.message.edit_text("💊 Готово! Спасибо ❤️")

@dp.message(F.text == "🏠 Главное меню")
async def go_main(message: Message):
    await message.answer("🏠 Главное меню", reply_markup=main_kb)

@dp.message(F.text.in_(["🔴 Менструация", "🟢 Фолликуляр", "🟡 Овуляция", "🟣 Лютеин"]))
async def handle_phase(message: Message):
    await message.answer(f"Выбрано: {message.text}", reply_markup=cycle_kb)

@dp.message(F.text == "🗑️ Удалить данные")
async def delete_data(message: Message):
    user_id = message.from_user.id
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cycles WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM symptoms WHERE user_id = ?", (user_id,))
        conn.commit()
    await message.answer("🗑️ Удалено", reply_markup=main_kb)

@dp.message(F.text == "🆘 Помощь")
async def help_cmd(message: Message):
    await message.answer(
        "ℹ️ Помощь:\n"
        "➕ Новый цикл — начать\n"
        "📊 Мой цикл — статус\n"
        "💊 Симптомы — отметить\n"
        "📈 Предсказание — даты\n"
        "🏠 Главное меню — вернуться",
        reply_markup=main_kb
    )

# === ВЕБХУКИ ===
async def on_startup(bot: Bot):
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🔌 Webhook удалён")

def create_app():
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot, on_startup=on_startup, on_shutdown=on_shutdown)
    return app

if __name__ == "__main__":
    init_db()
    app = create_app()
    port = int(os.getenv("PORT", 8000))
    web.run_app(app, host="0.0.0.0", port=port)
