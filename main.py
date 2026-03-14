import asyncio
import logging
import os
import json
import urllib.request
import dateparser
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from dotenv import load_dotenv

from sqlalchemy import create_engine, Column, Integer, String, Text, ForeignKey, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from pyrogram import Client, filters, idle
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove,
)

# === LOGGING ===
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
logging.getLogger("pyrogram").setLevel(logging.DEBUG)     # полный pyrogram debug
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
logger = logging.getLogger("CycleBot")

# === CONFIGURATION ===
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
API_ID    = int(os.getenv("API_ID", "0"))
API_HASH  = os.getenv("API_HASH", "")

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not found in .env!")
if not API_ID or not API_HASH:
    raise ValueError("API_ID and API_HASH are required! Get them at https://my.telegram.org")

# === DATABASE SCHEMA ===
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(String, primary_key=True)
    avg_cycle_length = Column(Integer, default=28)
    avg_period_duration = Column(Integer, default=5)
    takes_contraceptives = Column(String, default=None)   # 'yes' / 'no' / None
    contraceptive_start_date = Column(String, default=None)  # DD.MM.YYYY
    cycles = relationship("Cycle", back_populates="user", cascade="all, delete-orphan")

class Cycle(Base):
    __tablename__ = 'cycles'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey('users.id'))
    start_date = Column(String)   # DD.MM.YYYY
    end_date   = Column(String)   # DD.MM.YYYY
    date_added = Column(DateTime, default=datetime.now)
    user = relationship("User", back_populates="cycles")
    symptoms = relationship("Symptom", back_populates="cycle", cascade="all, delete-orphan")
    daily_intensities = relationship("DailyIntensity", back_populates="cycle", cascade="all, delete-orphan")

class DailyIntensity(Base):
    __tablename__ = 'daily_intensities'
    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id  = Column(Integer, ForeignKey('cycles.id'))
    date      = Column(String)   # DD.MM.YYYY
    intensity = Column(Integer)  # 1–5
    cycle = relationship("Cycle", back_populates="daily_intensities")

class Symptom(Base):
    __tablename__ = 'symptoms'
    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id = Column(Integer, ForeignKey('cycles.id'))
    date = Column(String)   # DD.MM.YYYY
    text = Column(Text)
    cycle = relationship("Cycle", back_populates="symptoms")

class IntimateEvent(Base):
    __tablename__ = 'intimate_events'
    id            = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id      = Column(Integer, ForeignKey('cycles.id'))
    date          = Column(String)   # DD.MM.YYYY
    activity_type = Column(String)   # 'sex' / 'masturbation' / 'toys' / 'all'
    had_orgasm    = Column(String)   # 'yes' / 'no'
    orgasm_count  = Column(Integer, default=0)
    cycle = relationship("Cycle", back_populates="intimate_events")

Cycle.intimate_events = relationship("IntimateEvent", back_populates="cycle", cascade="all, delete-orphan")

engine = create_engine('sqlite:///tracker.db')
Base.metadata.create_all(engine)
SessionFactory = sessionmaker(bind=engine)

@contextmanager
def get_db():
    db = SessionFactory()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# === PYROGRAM CLIENT ===
app = Client("cycle_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# === SIMPLE FSM ===
_states: dict[int, str]       = {}
_data:   dict[int, dict]      = {}

def get_state(uid: int) -> str | None:
    return _states.get(uid)

def set_state(uid: int, state: str):
    _states[uid] = state

def clear_state(uid: int):
    _states.pop(uid, None)
    _data.pop(uid, None)

def get_data(uid: int) -> dict:
    return _data.get(uid, {})

def update_data(uid: int, **kwargs):
    _data.setdefault(uid, {}).update(kwargs)

# === STATES ===
S_START_DATE       = "waiting_start_date"
S_END_DATE         = "waiting_end_date"
S_INTENSITY        = "waiting_intensity"
S_SYMPTOMS         = "waiting_symptoms"
S_INTIM_DATE       = "waiting_intimate_date"
S_INTIM_ACTIVITY   = "waiting_intimate_activity"
S_INTIM_ORGASM     = "waiting_intimate_orgasm"
S_INTIM_ORG_COUNT  = "waiting_intimate_orgasm_count"
S_CONTRA           = "waiting_contraceptive_question"

# === KEYBOARDS ===
MENU_BUTTONS = [
    "Добавить цикл ➕", "🔮 Предсказание", "📊 Мои циклы",
    "📝 Симптомы", "💋 Интим", "💊 Противозачаточные",
    "📋 Отчет врачу", "🗑️ Удалить данные",
]

def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Добавить цикл ➕")],
            [KeyboardButton("🔮 Предсказание")],
            [KeyboardButton("📊 Мои циклы"), KeyboardButton("📝 Симптомы")],
            [KeyboardButton("💋 Интим"), KeyboardButton("💊 Противозачаточные")],
            [KeyboardButton("📋 Отчет врачу"), KeyboardButton("🗑️ Удалить данные")],
        ],
        resize_keyboard=True,
    )

# === DATE PARSER ===
def parse_flexible_date(date_str: str):
    try:
        clean = date_str.strip().lower()
        settings = {
            'PREFER_DAY_OF_MONTH': 'first',
            'REQUIRE_PARTS': ['day', 'month'],
            'RELATIVE_BASE': datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
            'DATE_ORDER': 'DMY',
            'PREFER_DATES_FROM': 'past',
        }
        dt = dateparser.parse(clean, languages=['ru', 'en'], settings=settings)
        if not dt and '.' in clean:
            dt = dateparser.parse(f"{clean}.{datetime.now().year}", languages=['ru', 'en'], settings=settings)
        if not dt:
            cleaner = re.sub(r'[^a-zа-я0-9\s.\-\/]', '', clean)
            dt = dateparser.parse(cleaner, languages=['ru', 'en'], settings=settings)
        if dt:
            if dt < datetime.now() - timedelta(days=365 * 50):
                return "TOO_OLD"
        return dt
    except Exception as e:
        logger.error(f"parse_flexible_date error: {e}")
        return None

# === CYCLE HELPERS ===
def find_target_cycle(cycles, dt: datetime):
    """Find the cycle that a given date belongs to."""
    sorted_all = sorted(cycles, key=lambda c: datetime.strptime(c.start_date, "%d.%m.%Y"))
    target = None
    for i, c in enumerate(sorted_all):
        c_start = datetime.strptime(c.start_date, "%d.%m.%Y")
        if i + 1 < len(sorted_all):
            next_start = datetime.strptime(sorted_all[i + 1].start_date, "%d.%m.%Y")
            if c_start <= dt < next_start:
                target = c
                break
        else:
            if dt >= c_start:
                target = c
                break
    return target or (sorted_all[0] if sorted_all else None)

async def finish_cycle_saving(client, chat_id: int, user_id: str, start_date: str, end_date: str, intensities: dict = None):
    try:
        with get_db() as db:
            user = db.query(User).filter_by(id=user_id).first()
            if not user:
                user = User(id=user_id)
                db.add(user)
                db.commit()
                user = db.query(User).filter_by(id=user_id).first()

            cycle = db.query(Cycle).filter_by(user_id=user_id, start_date=start_date).first()
            if cycle:
                cycle.end_date = end_date
                db.query(DailyIntensity).filter_by(cycle_id=cycle.id).delete()
            else:
                cycle = Cycle(user_id=user_id, start_date=start_date, end_date=end_date)
                db.add(cycle)
            db.commit()

            if intensities:
                for d_str, val in intensities.items():
                    db.add(DailyIntensity(cycle_id=cycle.id, date=d_str, intensity=val))
                db.commit()

            all_cycles = db.query(Cycle).filter_by(user_id=user_id).all()
            if len(all_cycles) >= 2:
                sorted_c = sorted(all_cycles, key=lambda x: datetime.strptime(x.start_date, "%d.%m.%Y"))
                gaps, durs = [], []
                for i, c in enumerate(sorted_c):
                    s = datetime.strptime(c.start_date, "%d.%m.%Y")
                    e = datetime.strptime(c.end_date, "%d.%m.%Y")
                    durs.append((e - s).days + 1)
                    if i > 0:
                        prev_s = datetime.strptime(sorted_c[i - 1].start_date, "%d.%m.%Y")
                        gap = (s - prev_s).days
                        if 15 <= gap <= 60:
                            gaps.append(gap)
                if gaps:
                    user.avg_cycle_length = round(sum(gaps) / len(gaps))
                if durs:
                    user.avg_period_duration = round(sum(durs) / len(durs))
                db.commit()

    except Exception as e:
        logger.error(f"DB Error in finish_cycle_saving: {e}")
        await client.send_message(chat_id, "❌ Ошибка при сохранении в базу данных.")
        return

    clear_state(chat_id)
    await client.send_message(
        chat_id,
        f"✅ Сохранено!\n📅 Период: {start_date} — {end_date}",
        reply_markup=main_kb(),
    )

async def ask_intensity(client, chat_id: int, uid: int):
    data = get_data(uid)
    start_dt = datetime.strptime(data['start_date'], "%d.%m.%Y")
    end_dt   = datetime.strptime(data['end_date'],   "%d.%m.%Y")
    temp     = data.get('temp_intensities', {})

    current = start_dt
    while current.strftime("%d.%m.%Y") in temp:
        current += timedelta(days=1)
        if current > end_dt:
            await finish_cycle_saving(client, chat_id, str(uid), data['start_date'], data['end_date'], temp)
            return

    set_state(uid, S_INTENSITY)
    update_data(uid, current_intensity_date=current.strftime("%d.%m.%Y"))

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🩸",       callback_data="int_1"),
         InlineKeyboardButton("🩸🩸",     callback_data="int_2"),
         InlineKeyboardButton("🩸🩸🩸",   callback_data="int_3")],
        [InlineKeyboardButton("🩸🩸🩸🩸",   callback_data="int_4"),
         InlineKeyboardButton("🩸🩸🩸🩸🩸", callback_data="int_5")],
    ])
    await client.send_message(chat_id, f"Выберите обильность за {current.strftime('%d.%m.%Y')}:", reply_markup=kb)

# ============================================================
# DEBUG: RAW UPDATE + CATCH-ALL (group -1 — highest priority)
# ============================================================

@app.on_raw_update()
async def raw_update_logger(client, update, users, chats):
    update_name = type(update).__name__
    logger.debug(f"[RAW] {update_name}")
    print(f"[RAW UPDATE] {update_name}", flush=True)

from pyrogram.handlers import MessageHandler as _MH, CallbackQueryHandler as _CQH

async def _catch_all_msg(client, message):
    logger.info(f"[CATCH-ALL MSG] from={getattr(message.from_user, 'id', '?')} "
                f"text={message.text!r} type={message.chat.type if message.chat else '?'}")
    print(f"[CATCH-ALL MSG] {getattr(message.from_user, 'id', '?')} → {message.text!r}", flush=True)

async def _catch_all_cb(client, cb):
    logger.info(f"[CATCH-ALL CB] from={cb.from_user.id} data={cb.data!r}")
    print(f"[CATCH-ALL CB] {cb.from_user.id} → {cb.data!r}", flush=True)

app.add_handler(_MH(_catch_all_msg),  group=-1)
app.add_handler(_CQH(_catch_all_cb), group=-1)

# ============================================================
# COMMAND HANDLERS
# ============================================================

@app.on_message(filters.command("start"))
async def cmd_start(client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    logger.info(f"[/start] uid={uid} name={message.from_user.first_name}")
    try:
        clear_state(uid)
        with get_db() as db:
            user = db.query(User).filter_by(id=str(uid)).first()
            if not user:
                db.add(User(id=str(uid)))
                db.commit()
                logger.info(f"[/start] New user registered: {uid}")
                set_state(uid, S_CONTRA)
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Да",  callback_data="contra_yes"),
                    InlineKeyboardButton("❌ Нет", callback_data="contra_no"),
                ]])
                await message.reply("🌸 Добро пожаловать в трекер цикла!\n\n💊 Вы принимаете противозачаточные?", reply_markup=kb)
            else:
                logger.info(f"[/start] Returning user: {uid}")
                await message.reply("🌸 Добро пожаловать!", reply_markup=main_kb())
    except Exception as e:
        logger.exception(f"[/start] ERROR uid={uid}: {e}")

# ============================================================
# MAIN TEXT MESSAGE ROUTER
# ============================================================

@app.on_message(filters.text)
async def text_router(client, message: Message):
    # Игнорируем сообщения без from_user (каналы, боты)
    if not message.from_user:
        return
    uid   = message.from_user.id
    text  = message.text or ""
    state = get_state(uid)
    logger.info(f"[MSG] uid={uid} state={state!r} text={text!r}")

    # Игнорируем команды — они уже обработаны выше
    if text.startswith("/"):
        logger.debug(f"[MSG] Skipping command in text_router: {text!r}")
        return

    try:
        # Кнопки меню — всегда приоритет
        if text in MENU_BUTTONS:
            logger.info(f"[MENU] uid={uid} button={text!r}")
            clear_state(uid)
            await handle_menu(client, message, text)
            return

        # Роутинг по состоянию
        if state == S_START_DATE:
            await process_start_date(client, message)
        elif state == S_END_DATE:
            await process_end_date(client, message)
        elif state == S_SYMPTOMS:
            await process_symptoms(client, message)
        elif state == S_INTIM_DATE:
            await process_intimate_date(client, message)
        else:
            logger.info(f"[MSG] No state match for uid={uid}, sending menu")
            await message.reply("Выберите действие из меню:", reply_markup=main_kb())
    except Exception as e:
        logger.exception(f"[MSG] ERROR uid={uid} text={text!r}: {e}")
        await message.reply("❌ Внутренняя ошибка. Попробуйте ещё раз.")

async def handle_menu(client, message: Message, text: str):
    uid = message.from_user.id
    logger.debug(f"[HANDLE_MENU] uid={uid} text={text!r}")
    if text == "Добавить цикл ➕":
        set_state(uid, S_START_DATE)
        await message.reply("📅 Введите дату начала месячных (например: 15.08, вчера):")
    elif text == "🔮 Предсказание":
        await show_prediction(client, message)
    elif text == "📊 Мои циклы":
        await show_history(client, message)
    elif text == "📝 Симптомы":
        set_state(uid, S_SYMPTOMS)
        await message.reply("📝 Введите: Дата, симптомы\nПример: сегодня, боль в животе")
    elif text == "💋 Интим":
        set_state(uid, S_INTIM_DATE)
        await message.reply("📅 Введите дату (например: сегодня, вчера, 15.02):")
    elif text == "💊 Противозачаточные":
        await show_contraceptives(client, message)
    elif text == "📋 Отчет врачу":
        await generate_report(client, message)
    elif text == "🗑️ Удалить данные":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑️ УДАЛИТЬ ВСЁ", callback_data="confirm_delete_all")
        ]])
        await message.reply("⚠️ Вы уверены? Все данные будут безвозвратно удалены!", reply_markup=kb)

# ============================================================
# CYCLE FLOW
# ============================================================

async def process_start_date(client, message: Message):
    uid = message.from_user.id
    logger.info(f"[START_DATE] uid={uid} input={message.text!r}")
    dt = parse_flexible_date(message.text)
    logger.debug(f"[START_DATE] parsed={dt}")
    if dt == "TOO_OLD":
        return await message.reply("❌ Введите реалистичную дату (не слишком давнюю).")
    if not dt or dt > datetime.now():
        return await message.reply("❌ Неверная дата. Попробуйте ещё раз:")

    start_str = dt.strftime("%d.%m.%Y")
    update_data(uid, start_date=start_str)
    set_state(uid, S_END_DATE)
    logger.info(f"[START_DATE] uid={uid} saved start={start_str}, state→S_END_DATE")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{i} дн.", callback_data=f"dur_{i}") for i in range(3, 6)],
        [InlineKeyboardButton(f"{i} дн.", callback_data=f"dur_{i}") for i in range(6, 9)],
        [InlineKeyboardButton("Ещё идут", callback_data="period_ongoing")],
    ])
    await message.reply(f"✅ Начало: {start_str}\n📅 Когда закончились? Введите дату или выберите:", reply_markup=kb)

async def process_end_date(client, message: Message):
    uid  = message.from_user.id
    logger.info(f"[END_DATE] uid={uid} input={message.text!r}")
    data = get_data(uid)
    start_str = data.get("start_date")
    if not start_str:
        logger.warning(f"[END_DATE] uid={uid} no start_date in session!")
        return await message.reply("❌ Ошибка сессии. Начните заново.")

    end_dt = parse_flexible_date(message.text)
    logger.debug(f"[END_DATE] parsed={end_dt}")
    if end_dt == "TOO_OLD":
        return await message.reply("❌ Введите реалистичную дату.")
    start_dt = datetime.strptime(start_str, "%d.%m.%Y")
    if not end_dt or end_dt > datetime.now() or end_dt < start_dt:
        return await message.reply("❌ Неверная дата окончания. Попробуйте ещё раз:")

    end_str = end_dt.strftime("%d.%m.%Y")
    update_data(uid, end_date=end_str)
    logger.info(f"[END_DATE] uid={uid} saved end={end_str}, asking intensity")
    await ask_intensity(client, message.chat.id, uid)

# ============================================================
# SYMPTOMS FLOW
# ============================================================

async def process_symptoms(client, message: Message):
    uid = message.from_user.id
    logger.info(f"[SYMPTOMS] uid={uid} input={message.text!r}")
    try:
        parts = message.text.split(",", 1)
        dt = parse_flexible_date(parts[0])
        logger.debug(f"[SYMPTOMS] parsed date={dt}")
        if dt == "TOO_OLD":
            return await message.reply("❌ Введите реалистичную дату.")
        if not dt:
            return await message.reply("❌ Ошибка даты. Формат: дата, симптом")

        with get_db() as db:
            cycles = db.query(Cycle).filter_by(user_id=str(uid)).all()
            if not cycles:
                return await message.reply("❌ Сначала добавьте цикл!")
            target = find_target_cycle(cycles, dt)
            symptom_text = parts[1].strip() if len(parts) > 1 else ""
            db.add(Symptom(cycle_id=target.id, date=dt.strftime("%d.%m.%Y"), text=symptom_text))
            db.commit()
            logger.info(f"[SYMPTOMS] uid={uid} saved symptom='{symptom_text}' for cycle={target.id}")

        clear_state(uid)
        await message.reply(f"✅ Симптом записан за {dt.strftime('%d.%m.%Y')}", reply_markup=main_kb())
    except Exception as e:
        logger.exception(f"[SYMPTOMS] ERROR uid={uid}: {e}")
        await message.reply("❌ Ошибка формата. Пример: сегодня, боль")

# ============================================================
# INTIMATE FLOW
# ============================================================

async def process_intimate_date(client, message: Message):
    uid = message.from_user.id
    logger.info(f"[INTIM_DATE] uid={uid} input={message.text!r}")
    dt = parse_flexible_date(message.text)
    logger.debug(f"[INTIM_DATE] parsed={dt}")
    if dt == "TOO_OLD":
        return await message.reply("❌ Введите реалистичную дату.")
    if not dt or dt > datetime.now():
        return await message.reply("❌ Неверная дата. Попробуйте ещё раз:")

    update_data(uid, intimate_date=dt.strftime("%d.%m.%Y"))
    set_state(uid, S_INTIM_ACTIVITY)
    logger.info(f"[INTIM_DATE] uid={uid} date saved, state→S_INTIM_ACTIVITY")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Секс",          callback_data="intim_sex")],
        [InlineKeyboardButton("Мастурбация",   callback_data="intim_mast")],
        [InlineKeyboardButton("Секс-игрушки",  callback_data="intim_toys")],
        [InlineKeyboardButton("Секс + игрушки",callback_data="intim_all")],
    ])
    await message.reply("Выберите вид активности:", reply_markup=kb)

async def finish_intimate_event(client, chat_id: int, uid: int):
    data      = get_data(uid)
    date_str  = data['intimate_date']
    activity  = data['intimate_activity']
    had_org   = data['intimate_orgasm']
    org_count = data.get('intimate_orgasm_count', 0)

    try:
        with get_db() as db:
            cycles = db.query(Cycle).filter_by(user_id=str(uid)).all()
            if not cycles:
                await client.send_message(chat_id, "❌ Сначала добавьте цикл!")
                clear_state(uid)
                return
            dt     = datetime.strptime(date_str, "%d.%m.%Y")
            target = find_target_cycle(cycles, dt)
            db.add(IntimateEvent(
                cycle_id=target.id,
                date=date_str,
                activity_type=activity,
                had_orgasm=had_org,
                orgasm_count=org_count,
            ))
            db.commit()

        clear_state(uid)
        await client.send_message(chat_id, f"✅ Записано за {date_str}", reply_markup=main_kb())
    except Exception as e:
        logger.error(f"Intimate event error: {e}")
        await client.send_message(chat_id, "❌ Ошибка при сохранении.")

# ============================================================
# HISTORY & PREDICTION
# ============================================================

async def show_history(client, message: Message):
    uid = message.from_user.id
    with get_db() as db:
        cycles = db.query(Cycle).filter_by(user_id=str(uid)).all()
        if not cycles:
            return await message.reply("📭 История пуста. Добавьте первый цикл!", reply_markup=main_kb())

        sorted_c = sorted(cycles, key=lambda x: datetime.strptime(x.start_date, "%d.%m.%Y"))
        text = "📊 Мои циклы:\n\n"
        for i, c in enumerate(sorted_c, 1):
            text += f"{i}. {c.start_date} — {c.end_date}\n"
            if c.daily_intensities:
                for d in sorted(c.daily_intensities, key=lambda x: datetime.strptime(x.date, "%d.%m.%Y")):
                    text += f"   💧 {d.date}: {'🩸' * d.intensity}\n"
            for s in sorted(c.symptoms, key=lambda x: datetime.strptime(x.date, "%d.%m.%Y")):
                text += f"   • {s.date}: {s.text}\n"
            for ie in sorted(c.intimate_events, key=lambda x: datetime.strptime(x.date, "%d.%m.%Y")):
                atype = {"sex": "Секс", "masturbation": "Мастурбация", "toys": "Секс-игрушки", "all": "Секс + игрушки"}.get(ie.activity_type, ie.activity_type)
                org   = f" (оргазм: {ie.orgasm_count})" if ie.had_orgasm == 'yes' else ""
                text += f"   💋 {atype}{org} | {ie.date}\n"
        text = text[:4096]  # Telegram message limit
    await message.reply(text, reply_markup=main_kb())

async def show_prediction(client, message: Message):
    uid = message.from_user.id
    with get_db() as db:
        user   = db.query(User).filter_by(id=str(uid)).first()
        cycles = db.query(Cycle).filter_by(user_id=str(uid)).all()
        if not cycles:
            return await message.reply("❌ Нужен хотя бы один цикл для предсказания.")

        last_c      = max(cycles, key=lambda x: datetime.strptime(x.start_date, "%d.%m.%Y"))
        last_start  = datetime.strptime(last_c.start_date, "%d.%m.%Y")
        avg_cycle   = user.avg_cycle_length or 28
        next_start  = last_start + timedelta(days=avg_cycle)
        pms_start   = next_start - timedelta(days=7)
        today       = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        days_left   = (next_start - today).days

        text  = "🔮 **Ваше предсказание:**\n\n"
        text += f"📅 Следующие месячные: **{next_start.strftime('%d.%m.%Y')}**\n"
        if days_left > 0:
            text += f"⏳ Осталось дней: **{days_left}**\n"
        elif days_left == 0:
            text += "✨ Ожидаются сегодня!\n"
        else:
            text += f"⚠️ Задержка: **{abs(days_left)}** дн.\n"
        text += f"🌩 ПМС может начаться: **{pms_start.strftime('%d.%m.%Y')}**\n"
        text += f"🔄 Средний цикл: **{avg_cycle}** дн.\n"
        text += f"\n_Расчёт основан на вашей истории_"

    await message.reply(text, reply_markup=main_kb())

# ============================================================
# CONTRACEPTIVES
# ============================================================

async def show_contraceptives(client, message: Message):
    uid = message.from_user.id
    with get_db() as db:
        user = db.query(User).filter_by(id=str(uid)).first()
        if not user:
            return await message.reply("❌ Пользователь не найден")

        if user.takes_contraceptives == "yes":
            start = user.contraceptive_start_date or "неизвестно"
            msg = f"✅ Вы принимаете противозачаточные\nНачало приёма: {start}\n\nЧто хотите сделать?"
            kb  = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Изменить ответ", callback_data="manage_contra_change")]])
        elif user.takes_contraceptives == "no":
            msg = "❌ Вы не принимаете противозачаточные\n\nХотите начать приём?"
            kb  = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Начала принимать", callback_data="manage_contra_start")]])
        else:
            msg = "❓ Ответ не указан\n\nВы принимаете противозачаточные?"
            kb  = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да",  callback_data="contra_yes"),
                InlineKeyboardButton("❌ Нет", callback_data="contra_no"),
            ]])
    await message.reply(msg, reply_markup=kb)

# ============================================================
# REPORT (TEXT FORMAT)
# ============================================================

async def generate_report(client, message: Message):
    uid = message.from_user.id
    with get_db() as db:
        user   = db.query(User).filter_by(id=str(uid)).first()
        cycles = db.query(Cycle).filter_by(user_id=str(uid)).all()
        if not cycles:
            return await message.reply("❌ Добавьте циклы для создания отчёта")

        sorted_c   = sorted(cycles, key=lambda x: datetime.strptime(x.start_date, "%d.%m.%Y"))
        date_now   = datetime.now().strftime("%d.%m.%Y %H:%M")
        contra_str = "Да" if user.takes_contraceptives == "yes" else "Нет" if user.takes_contraceptives == "no" else "Не указано"

        lines = [
            "╔══════════════════════════════╗",
            "║   ОТЧЁТ ДЛЯ ГИНЕКОЛОГА      ║",
            "╚══════════════════════════════╝",
            f"Дата создания: {date_now}",
            "",
            "── СТАТИСТИКА ──────────────────",
            f"Всего циклов:               {len(cycles)}",
            f"Средняя длина цикла:        {user.avg_cycle_length or 28} дней",
            f"Средняя продолжительность:  {user.avg_period_duration or 5} дней",
            f"Противозачаточные:          {contra_str}",
        ]
        if user.takes_contraceptives == "yes" and user.contraceptive_start_date:
            lines.append(f"Начало приёма:              {user.contraceptive_start_date}")

        lines += ["", "── ИСТОРИЯ ЦИКЛОВ ──────────────"]
        for i, c in enumerate(sorted_c, 1):
            s_dt = datetime.strptime(c.start_date, "%d.%m.%Y")
            e_dt = datetime.strptime(c.end_date,   "%d.%m.%Y")
            dur  = (e_dt - s_dt).days + 1
            ints = sorted(c.daily_intensities, key=lambda x: datetime.strptime(x.date, "%d.%m.%Y"))
            avg_int = round(sum(d.intensity for d in ints) / len(ints)) if ints else 0
            intensity_str = "🩸" * avg_int if avg_int else "—"
            lines.append(f"{i:2}. {c.start_date} — {c.end_date}  ({dur} дн.)  {intensity_str}")
            for d in ints:
                lines.append(f"     {d.date}: {'🩸' * d.intensity}")

        all_symptoms = [(s.date, s.text) for c in sorted_c for s in c.symptoms]
        if all_symptoms:
            lines += ["", "── СИМПТОМЫ ────────────────────"]
            for date, text in sorted(all_symptoms, key=lambda x: datetime.strptime(x[0], "%d.%m.%Y")):
                lines.append(f"  {date}: {text}")

        all_intim = [(ie.date, ie.activity_type, ie.had_orgasm, ie.orgasm_count) for c in sorted_c for ie in c.intimate_events]
        if all_intim:
            lines += ["", "── ПОЛОВАЯ АКТИВНОСТЬ ──────────"]
            atype_map = {"sex": "Секс", "masturbation": "Мастурбация", "toys": "Игрушки", "all": "Секс+игрушки"}
            for date, atype, had_org, org_cnt in sorted(all_intim, key=lambda x: datetime.strptime(x[0], "%d.%m.%Y")):
                org_str = f", оргазм: {org_cnt}" if had_org == "yes" else ""
                lines.append(f"  {date}: {atype_map.get(atype, atype)}{org_str}")

        lines += ["", "────────────────────────────────",
                  "Отчёт сформирован автоматически"]

        report_text = "\n".join(lines)
        # Telegram max = 4096, send in chunks if needed
        for chunk_start in range(0, len(report_text), 4000):
            await message.reply(f"```\n{report_text[chunk_start:chunk_start+4000]}\n```")

# ============================================================
# CALLBACK QUERY HANDLER
# ============================================================

@app.on_callback_query()
async def callback_router(client, cb: CallbackQuery):
    uid  = cb.from_user.id
    data = cb.data
    logger.info(f"[CB] uid={uid} data={data!r}")
    try:
        # --- Duration buttons ---
        if data.startswith("dur_"):
            duration  = int(data.split("_")[1])
            d         = get_data(uid)
            start_str = d.get("start_date")
            if not start_str:
                return await cb.answer("Ошибка. Начните заново.", show_alert=True)
            start_dt  = datetime.strptime(start_str, "%d.%m.%Y")
            end_dt    = min(start_dt + timedelta(days=duration - 1), datetime.now())
            update_data(uid, end_date=end_dt.strftime("%d.%m.%Y"))
            logger.info(f"[CB:dur] uid={uid} duration={duration} end={end_dt.strftime('%d.%m.%Y')}")
            try:
                await cb.message.delete()
            except Exception:
                pass
            await ask_intensity(client, cb.message.chat.id, uid)
            await cb.answer()

        elif data == "period_ongoing":
            clear_state(uid)
            logger.info(f"[CB:ongoing] uid={uid}")
            await cb.message.edit_text("📝 Записала начало! Когда закончатся — добавьте запись снова с той же датой начала.")
            await cb.answer()

        # --- Intensity buttons ---
        elif data.startswith("int_"):
            intensity = int(data.split("_")[1])
            d         = get_data(uid)
            date_str  = d.get('current_intensity_date')
            temp      = d.get('temp_intensities', {})
            temp[date_str] = intensity
            update_data(uid, temp_intensities=temp)
            logger.info(f"[CB:int] uid={uid} date={date_str} intensity={intensity}")
            try:
                await cb.message.delete()
            except Exception:
                pass
            await ask_intensity(client, cb.message.chat.id, uid)
            await cb.answer()

        # --- Contraceptive buttons ---
        elif data in ("contra_yes", "contra_no"):
            answer = "yes" if data == "contra_yes" else "no"
            logger.info(f"[CB:contra] uid={uid} answer={answer}")
            with get_db() as db:
                user = db.query(User).filter_by(id=str(uid)).first()
                if user:
                    user.takes_contraceptives = answer
                    if answer == "yes":
                        user.contraceptive_start_date = datetime.now().strftime("%d.%m.%Y")
                    db.commit()
            clear_state(uid)
            label = "принимаете" if answer == "yes" else "не принимаете"
            await cb.message.edit_text(f"✅ Записано: вы {label} противозачаточные")
            await client.send_message(cb.message.chat.id, "🌸 Трекер готов к работе!", reply_markup=main_kb())
            await cb.answer()

        elif data == "manage_contra_change":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да",  callback_data="contra_yes"),
                InlineKeyboardButton("❌ Нет", callback_data="contra_no"),
            ]])
            await cb.message.edit_text("Вы принимаете противозачаточные?", reply_markup=kb)
            await cb.answer()

        elif data == "manage_contra_start":
            with get_db() as db:
                user = db.query(User).filter_by(id=str(uid)).first()
                if user:
                    user.takes_contraceptives = "yes"
                    user.contraceptive_start_date = datetime.now().strftime("%d.%m.%Y")
                    db.commit()
                    logger.info(f"[CB:contra_start] uid={uid} started on {user.contraceptive_start_date}")
                    await cb.message.edit_text(f"✅ Вы начали принимать противозачаточные с {user.contraceptive_start_date}")
            await cb.answer()

        # --- Intimate activity buttons ---
        elif data.startswith("intim_"):
            act_map = {"sex": "sex", "mast": "masturbation", "toys": "toys", "all": "all"}
            key     = data.split("_")[1]
            activity = act_map.get(key, key)
            update_data(uid, intimate_activity=activity)
            set_state(uid, S_INTIM_ORGASM)
            logger.info(f"[CB:intim] uid={uid} activity={activity}")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да",  callback_data="org_yes"),
                InlineKeyboardButton("❌ Нет", callback_data="org_no"),
            ]])
            await cb.message.edit_text("Был ли оргазм?", reply_markup=kb)
            await cb.answer()

        elif data.startswith("org_"):
            had = data.split("_")[1]
            update_data(uid, intimate_orgasm=had)
            logger.info(f"[CB:org] uid={uid} had_orgasm={had}")
            if had == "yes":
                set_state(uid, S_INTIM_ORG_COUNT)
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(str(i), callback_data=f"orgcount_{i}") for i in range(1, 6)],
                    [InlineKeyboardButton(str(i), callback_data=f"orgcount_{i}") for i in range(6, 11)],
                ])
                await cb.message.edit_text("Сколько раз?", reply_markup=kb)
            else:
                update_data(uid, intimate_orgasm_count=0)
                try:
                    await cb.message.delete()
                except Exception:
                    pass
                await finish_intimate_event(client, cb.message.chat.id, uid)
            await cb.answer()

        elif data.startswith("orgcount_"):
            count = int(data.split("_")[1])
            update_data(uid, intimate_orgasm_count=count)
            logger.info(f"[CB:orgcount] uid={uid} count={count}")
            try:
                await cb.message.delete()
            except Exception:
                pass
            await finish_intimate_event(client, cb.message.chat.id, uid)
            await cb.answer()

        # --- Delete confirmation ---
        elif data == "confirm_delete_all":
            logger.info(f"[CB:delete] uid={uid} deleting all data")
            with get_db() as db:
                user = db.query(User).filter_by(id=str(uid)).first()
                if user:
                    db.delete(user)
                    db.commit()
            await cb.message.edit_text("🗑️ Все данные удалены.")
            await client.send_message(cb.message.chat.id, "Введите /start чтобы начать заново.")
            await cb.answer()

        else:
            logger.warning(f"[CB] Unhandled callback: {data!r} from uid={uid}")
            await cb.answer()

    except Exception as e:
        logger.exception(f"[CB] ERROR uid={uid} data={data!r}: {e}")
        try:
            await cb.answer("❌ Внутренняя ошибка", show_alert=True)
        except Exception:
            pass

# ============================================================
# NOTIFICATIONS
# ============================================================

async def check_notifications():
    await asyncio.sleep(10)  # Wait for bot to fully start
    while True:
        try:
            now   = datetime.now()
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)

            with get_db() as db:
                users = db.query(User).all()
                for user in users:
                    try:
                        uid = int(user.id)
                        cycles = db.query(Cycle).filter_by(user_id=user.id).all()

                        # 09:00 — PMS & period predictions
                        if now.hour == 9 and now.minute == 0 and cycles:
                            last_c     = max(cycles, key=lambda x: datetime.strptime(x.start_date, "%d.%m.%Y"))
                            last_start = datetime.strptime(last_c.start_date, "%d.%m.%Y")
                            avg_cycle  = user.avg_cycle_length or 28
                            next_start = last_start + timedelta(days=avg_cycle)
                            pms_start  = next_start - timedelta(days=7)

                            if today == pms_start:
                                await app.send_message(uid, "🌸 Сегодня может начаться ПМС. Берегите себя!")
                            if today == next_start:
                                await app.send_message(uid, "🩸 По расчёту, сегодня должны начаться месячные. Не забудьте добавить новый цикл!")

                        # 20:00 — contraceptive reminder
                        if user.takes_contraceptives == "yes" and now.hour == 20 and now.minute == 0:
                            await app.send_message(uid, "💊 Напоминание: не забудьте принять противозачаточные!")

                        # Every 6h — period care reminder (during menstruation)
                        if now.minute == 0 and now.hour % 6 == 0 and cycles:
                            last_c   = max(cycles, key=lambda x: datetime.strptime(x.start_date, "%d.%m.%Y"))
                            last_end = datetime.strptime(last_c.end_date, "%d.%m.%Y")
                            if today <= last_end:
                                await app.send_message(uid, "🩸 Не забудьте сменить прокладку или тампон!")

                    except Exception as e:
                        logger.error(f"Notification error for user {user.id}: {e}")

        except Exception as e:
            logger.error(f"check_notifications error: {e}")

        await asyncio.sleep(60)

# ============================================================
# WEBHOOK CLEANUP (called before MTProto start)
# ============================================================

def delete_webhook_sync():
    """Delete any active Bot API webhook so MTProto can receive updates."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=false"
        req = urllib.request.Request(url, headers={"User-Agent": "CycleBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("result"):
                logger.info("✅ Webhook deleted (or was not set)")
            else:
                logger.warning(f"deleteWebhook response: {data}")
    except Exception as e:
        logger.warning(f"Could not delete webhook (non-fatal): {e}")

def check_webhook_sync():
    """Log current webhook info for diagnostics."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo"
        req = urllib.request.Request(url, headers={"User-Agent": "CycleBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            info = data.get("result", {})
            wh_url = info.get("url", "")
            pending = info.get("pending_update_count", 0)
            if wh_url:
                logger.warning(f"⚠️  Active webhook found: {wh_url!r} (pending={pending})")
            else:
                logger.info(f"✅ No webhook active. Pending updates: {pending}")
    except Exception as e:
        logger.warning(f"Could not check webhook: {e}")

# ============================================================
# HTTP BOT API POLLING FALLBACK
# Runs alongside MTProto; guarantees delivery even if MTProto
# update-push is not working in this environment.
# ============================================================

async def http_polling_loop():
    """
    Parallel polling via Telegram HTTP Bot API (getUpdates).
    Dispatches updates directly into the same pyrogram handlers.
    """
    offset = 0
    base_url = f"https://api.telegram.org/bot{BOT_TOKEN}"
    logger.info("[HTTP-POLL] Starting HTTP Bot API polling fallback…")

    while True:
        try:
            poll_url = f"{base_url}/getUpdates?offset={offset}&timeout=20&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D"
            req = urllib.request.Request(poll_url, headers={"User-Agent": "CycleBot/1.0"})
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(
                None,
                lambda: urllib.request.urlopen(req, timeout=25).read()
            )
            data = json.loads(raw)
            updates = data.get("result", [])

            for upd in updates:
                offset = upd["update_id"] + 1
                uid = None
                text = None
                cb_data = None

                if "message" in upd:
                    msg = upd["message"]
                    uid = msg.get("from", {}).get("id")
                    text = msg.get("text", "")
                    logger.info(f"[HTTP-POLL MSG] uid={uid} text={text!r}")
                    print(f"[HTTP-POLL MSG] uid={uid} text={text!r}", flush=True)
                    await _dispatch_http_message(msg)

                elif "callback_query" in upd:
                    cb = upd["callback_query"]
                    uid = cb.get("from", {}).get("id")
                    cb_data = cb.get("data", "")
                    logger.info(f"[HTTP-POLL CB] uid={uid} data={cb_data!r}")
                    print(f"[HTTP-POLL CB] uid={uid} data={cb_data!r}", flush=True)
                    await _dispatch_http_callback(cb)

        except asyncio.CancelledError:
            logger.info("[HTTP-POLL] Polling cancelled.")
            break
        except Exception as e:
            logger.warning(f"[HTTP-POLL] Error: {e}")
            await asyncio.sleep(3)

async def _send_http(chat_id: int, text: str, reply_markup=None):
    """Send a message via HTTP Bot API."""
    base_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base_url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "CycleBot/1.0",
    })
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10).read())
    except Exception as e:
        logger.warning(f"[HTTP-POLL] sendMessage error: {e}")

class _FakeClient:
    """Minimal client shim for passing to handlers that call client.send_message."""
    async def send_message(self, chat_id, text, **kwargs):
        rm = None
        if "reply_markup" in kwargs:
            rm = _serialize_markup(kwargs["reply_markup"])
        await _send_http(chat_id, text, rm)
    async def get_me(self):
        return None

_fake_client = _FakeClient()

def _serialize_markup(markup):
    """Convert pyrogram markup objects to Bot API JSON-compatible dict."""
    if markup is None:
        return None
    if isinstance(markup, InlineKeyboardMarkup):
        return {
            "inline_keyboard": [
                [{"text": btn.text, "callback_data": btn.callback_data} for btn in row]
                for row in markup.inline_keyboard
            ]
        }
    if isinstance(markup, ReplyKeyboardMarkup):
        return {
            "keyboard": [[{"text": btn.text} for btn in row] for row in markup.keyboard],
            "resize_keyboard": True,
        }
    return None

class _FakeMessage:
    """Minimal Message shim for HTTP-dispatched messages."""
    def __init__(self, raw_msg):
        self._raw = raw_msg
        self.text = raw_msg.get("text", "")
        self.chat = type("Chat", (), {
            "id": raw_msg["chat"]["id"],
            "type": raw_msg["chat"].get("type", "private"),
        })()
        self.from_user = type("User", (), {
            "id": raw_msg.get("from", {}).get("id"),
            "first_name": raw_msg.get("from", {}).get("first_name", ""),
        })() if raw_msg.get("from") else None

    async def reply(self, text, reply_markup=None, **kwargs):
        rm = _serialize_markup(reply_markup)
        await _send_http(self._raw["chat"]["id"], text, rm)

    async def edit_text(self, text, reply_markup=None, **kwargs):
        """Edit via Bot API editMessageText."""
        base_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id": self._raw["chat"]["id"],
            "message_id": self._raw.get("message_id"),
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(_serialize_markup(reply_markup))
        data = json.dumps(payload).encode()
        req = urllib.request.Request(base_url, data=data, headers={
            "Content-Type": "application/json", "User-Agent": "CycleBot/1.0",
        })
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10).read())
        except Exception as e:
            logger.warning(f"[HTTP] edit_text error: {e}")

    async def delete(self):
        """Delete via Bot API deleteMessage."""
        base_url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
        payload = {"chat_id": self._raw["chat"]["id"], "message_id": self._raw.get("message_id")}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(base_url, data=data, headers={
            "Content-Type": "application/json", "User-Agent": "CycleBot/1.0",
        })
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10).read())
        except Exception:
            pass

class _FakeCallback:
    """Minimal CallbackQuery shim for HTTP-dispatched callbacks."""
    def __init__(self, raw_cb):
        self._raw = raw_cb
        self.data = raw_cb.get("data", "")
        self.from_user = type("User", (), {
            "id": raw_cb.get("from", {}).get("id"),
            "first_name": raw_cb.get("from", {}).get("first_name", ""),
        })()
        raw_msg = raw_cb.get("message", {})
        self.message = _FakeMessage(raw_msg) if raw_msg else None

    async def answer(self, text="", show_alert=False):
        base_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
        payload = {"callback_query_id": self._raw["id"], "text": text, "show_alert": show_alert}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(base_url, data=data, headers={
            "Content-Type": "application/json", "User-Agent": "CycleBot/1.0",
        })
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10).read())
        except Exception as e:
            logger.warning(f"[HTTP-POLL] answerCallbackQuery error: {e}")

async def _dispatch_http_message(raw_msg):
    try:
        msg = _FakeMessage(raw_msg)
        text = msg.text or ""
        uid = msg.from_user.id if msg.from_user else None
        if uid is None:
            return
        state = get_state(uid)
        logger.info(f"[HTTP-DISPATCH] uid={uid} text={text!r} state={state!r}")

        if text.startswith("/start"):
            await cmd_start(_fake_client, msg)
        elif text in MENU_BUTTONS:
            clear_state(uid)
            await handle_menu(_fake_client, msg, text)
        elif state == S_START_DATE:
            await process_start_date(_fake_client, msg)
        elif state == S_END_DATE:
            await process_end_date(_fake_client, msg)
        elif state == S_SYMPTOMS:
            await process_symptoms(_fake_client, msg)
        elif state == S_INTIM_DATE:
            await process_intimate_date(_fake_client, msg)
        else:
            await msg.reply("Выберите действие из меню:", reply_markup=main_kb())
    except Exception as e:
        logger.exception(f"[HTTP-DISPATCH] message error: {e}")

async def _dispatch_http_callback(raw_cb):
    try:
        cb = _FakeCallback(raw_cb)
        await callback_router(_fake_client, cb)
    except Exception as e:
        logger.exception(f"[HTTP-DISPATCH] callback error: {e}")

# ============================================================
# MAIN
# ============================================================

async def main():
    # Step 1: Delete any webhook that would block update delivery
    logger.info("Checking/deleting Bot API webhook…")
    delete_webhook_sync()
    check_webhook_sync()

    async with app:
        me = await app.get_me()
        logger.info(f"✅ Bot online: @{me.username} (id={me.id})")
        print(f"\n✅ Бот @{me.username} запущен! Нажми Ctrl+C для остановки.\n", flush=True)

        # HTTP polling delivers messages even if MTProto push isn't working
        asyncio.create_task(http_polling_loop())
        asyncio.create_task(check_notifications())
        logger.info("Polling + notification tasks scheduled. Awaiting updates…")
        await idle()

    logger.info("Bot stopped.")

if __name__ == "__main__":
    logger.info("Starting via asyncio.run(main())…")
    asyncio.run(main())
