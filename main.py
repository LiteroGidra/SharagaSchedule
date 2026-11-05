import asyncio
import json
import logging
import math
import os
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))
SCHEDULE_FILE = Path(os.getenv("SCHEDULE_FILE", Path(__file__).with_name("schedule.json")))
# Дата любого дня недели, которая считается ЧИСЛИТЕЛЕМ (нужно только если в расписании есть числитель/знаменатель)
SEMESTER_START = date.fromisoformat(os.getenv("SEMESTER_START", "2026-09-01"))
# Если между парами пауза не больше этого значения (мин) — это перемена, иначе «окно»
BREAK_MAX_MIN = 20

DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

BTN_NOW = "⏱ Что сейчас?"
BTN_TODAY = "📅 Сегодня"
BTN_TOMORROW = "➡️ Завтра"
BTN_WEEK = "🗓 На неделю"

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_NOW)],
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
        [KeyboardButton(text=BTN_WEEK)],
    ],
    resize_keyboard=True,
)

with open(SCHEDULE_FILE, encoding="utf-8") as f:
    DATA = json.load(f)
SCHEDULE = DATA["schedule"]


# ---------- работа с данными ----------

def now() -> datetime:
    return datetime.now(TZ)


def week_kind(d: date) -> str:
    start_monday = SEMESTER_START - timedelta(days=SEMESTER_START.weekday())
    d_monday = d - timedelta(days=d.weekday())
    weeks = (d_monday - start_monday).days // 7
    return "numerator" if weeks % 2 == 0 else "denominator"


def lessons_for(d: date) -> list[dict]:
    """Пары на конкретную дату (с учётом числителя/знаменателя), отсортированные по времени."""
    day = SCHEDULE.get(DAY_KEYS[d.weekday()], {"lessons": []})
    kind = week_kind(d)
    res = [l for l in day["lessons"] if l.get("week", "every") in ("every", kind)]
    return sorted(res, key=lambda l: l["start"])


def dt(d: date, hhmm: str) -> datetime:
    return datetime.combine(d, time.fromisoformat(hhmm), tzinfo=TZ)


def merge(lessons: list[dict]) -> list[dict]:
    """Склеивает подряд идущие одинаковые пары (сдвоенные/строенные) в один блок."""
    res: list[dict] = []
    for l in lessons:
        if (
            res
            and res[-1]["_last"] + 1 == l["pair"]
            and all(res[-1].get(k) == l.get(k) for k in ("subject", "type", "room", "teacher", "group", "week"))
        ):
            res[-1]["end"] = l["end"]
            res[-1]["_last"] = l["pair"]
        else:
            res.append({**l, "_last": l["pair"]})
    return res


def minutes_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "минут"
    return {1: "минута", 2: "минуты", 3: "минуты", 4: "минуты"}.get(n % 10, "минут")


def fmt_min(n: int) -> str:
    return f"{n} {minutes_word(n)}"


def ceil_minutes(delta: timedelta) -> int:
    return max(1, math.ceil(delta.total_seconds() / 60))


# ---------- форматирование ----------

def fmt_lesson(l: dict, with_time: bool = True) -> str:
    head = f"<b>{escape(l['start'])}–{escape(l['end'])}</b> — " if with_time else ""
    lines = [f"{head}<b>{escape(l['subject'])}</b>"]
    details = [f"📍 {escape(l['room'])}", escape(l["type"])]
    if l.get("group") and l["group"] != "вся группа":
        details.append(escape(l["group"]))
    if l.get("week") == "numerator":
        details.append("числитель")
    elif l.get("week") == "denominator":
        details.append("знаменатель")
    lines.append("   " + " · ".join(details))
    if l.get("teacher"):
        lines.append(f"   👤 {escape(l['teacher'])}")
    return "\n".join(lines)


def fmt_day(d: date, title: str | None = None) -> str:
    title = title or f"{DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}"
    lessons = merge(lessons_for(d))
    if not lessons:
        return f"📅 <b>{title}</b>\n\nПар нет 🎉"
    body = "\n\n".join(fmt_lesson(l) for l in lessons)
    return f"📅 <b>{title}</b>\n\n{body}"


def fmt_week(today: date) -> str:
    monday = today - timedelta(days=today.weekday())
    parts = []
    for i in range(6):  # Пн–Сб
        d = monday + timedelta(days=i)
        mark = " 👈" if d == today else ""
        lessons = merge(lessons_for(d))
        header = f"<b>{DAY_NAMES[i]}</b>{mark}"
        if lessons:
            parts.append(header + "\n" + "\n".join(
                f"• {escape(l['start'])}–{escape(l['end'])} {escape(l['subject'])} ({escape(l['room'])}, {escape(l['type'])})"
                for l in lessons
            ))
        else:
            parts.append(header + "\n• пар нет")
    return "🗓 <b>Расписание на неделю</b>\n\n" + "\n\n".join(parts)


def find_next_lesson_after(moment: datetime):
    """Ближайшая пара после момента (ищет на 14 дней вперёд). Возвращает (дата, пара) или None."""
    for add in range(0, 15):
        d = (moment + timedelta(days=add)).date()
        for l in lessons_for(d):
            if dt(d, l["start"]) > moment:
                return d, l
    return None


def what_now() -> str:
    n = now()
    today = n.date()
    lessons = lessons_for(today)

    # 1. Идёт пара?
    for l in lessons:
        start, end = dt(today, l["start"]), dt(today, l["end"])
        if start <= n < end:
            left = ceil_minutes(end - n)
            text = f"📖 <b>Сейчас идёт пара</b>\n\n{fmt_lesson(l, with_time=False)}\n\n⏳ До конца: <b>{fmt_min(left)}</b> (до {escape(l['end'])})"
            nxt = next((x for x in lessons if x["start"] >= l["end"] and x is not l), None)
            if nxt and nxt["start"] != l["end"]:
                text += f"\n\nДальше: {escape(nxt['start'])} — {escape(nxt['subject'])}, {escape(nxt['room'])}"
            return text

    # 2. Пара не идёт — ищем следующую
    nxt = find_next_lesson_after(n)
    if nxt is None:
        return "😴 Ближайших пар в расписании не нашёл."
    nd, nl = nxt
    nstart = dt(nd, nl["start"])

    if nd == today:
        prev_end = max((dt(today, x["end"]) for x in lessons if dt(today, x["end"]) <= n), default=None)
        left = ceil_minutes(nstart - n)
        if prev_end is None:
            title = "🌅 <b>Пары ещё не начались</b>"
        elif (nstart - prev_end) <= timedelta(minutes=BREAK_MAX_MIN):
            title = "🚶 <b>Сейчас перемена</b>"
        else:
            title = "🪟 <b>Сейчас окно</b>"
        return (
            f"{title}\n\n⏳ До следующей пары: <b>{fmt_min(left)}</b> (в {escape(nl['start'])})\n\n"
            f"Следующая:\n{fmt_lesson(nl, with_time=False)}"
        )

    # Сегодня пар больше нет
    if lessons:
        head = "✅ <b>Пары на сегодня закончились</b>"
    else:
        head = "🎉 <b>Сегодня пар нет</b>"
    when = "завтра" if nd == today + timedelta(days=1) else f"{DAY_NAMES[nd.weekday()].lower()}, {nd.strftime('%d.%m')}"
    return f"{head}\n\nСледующая пара — {when} в {escape(nl['start'])}:\n{fmt_lesson(nl, with_time=False)}"


# ---------- хендлеры ----------

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот с расписанием 📚\nВыбери действие в меню ниже.",
        reply_markup=MENU,
    )


@dp.message(Command("now"))
@dp.message(F.text == BTN_NOW)
async def h_now(message: Message):
    await message.answer(what_now(), reply_markup=MENU)


@dp.message(Command("today"))
@dp.message(F.text == BTN_TODAY)
async def h_today(message: Message):
    await message.answer(fmt_day(now().date(), f"Сегодня, {DAY_NAMES[now().weekday()]}"), reply_markup=MENU)


@dp.message(Command("tomorrow"))
@dp.message(F.text == BTN_TOMORROW)
async def h_tomorrow(message: Message):
    d = now().date() + timedelta(days=1)
    await message.answer(fmt_day(d, f"Завтра, {DAY_NAMES[d.weekday()]}"), reply_markup=MENU)


@dp.message(Command("week"))
@dp.message(F.text == BTN_WEEK)
async def h_week(message: Message):
    await message.answer(fmt_week(now().date()), reply_markup=MENU)


@dp.message()
async def fallback(message: Message):
    await message.answer("Выбери действие в меню 👇", reply_markup=MENU)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Добавь его в файл .env (BOT_TOKEN=...) или в переменные окружения.")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
