import asyncio
import json
import logging
import math
import os
import uuid
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

BASE = Path(__file__).parent
BOT_TOKEN = os.getenv("BOT_TOKEN")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))
SCHEDULE_FILE = Path(os.getenv("SCHEDULE_FILE", BASE / "schedule.json"))
HW_FILE = Path(os.getenv("HOMEWORK_FILE", BASE / "homework.json"))
# Дата любого дня недели, которая считается ЧИСЛИТЕЛЕМ (нужно, только если есть числитель/знаменатель)
SEMESTER_START = date.fromisoformat(os.getenv("SEMESTER_START", "2026-09-01"))
# Кому разрешено пользоваться ботом (id через запятую). Пусто = всем.
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
# Пауза между парами не больше этого (мин) — перемена, иначе «окно»
BREAK_MAX_MIN = 20

DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
TYPES = ["лекция", "практика", "лабораторная"]
WEEKS = [("every", "Каждая неделя"), ("numerator", "Числитель"), ("denominator", "Знаменатель")]
FIELDS = {
    "subject": "Предмет", "type": "Тип", "room": "Кабинет", "teacher": "Преподаватель",
    "group": "Группа", "week": "Неделя", "pair": "№ пары",
}
DEFAULT_PAIR_TIMES = {
    1: ("08:00", "08:50"), 2: ("08:55", "09:45"), 3: ("09:55", "10:45"), 4: ("10:50", "11:40"),
    5: ("12:00", "12:50"), 6: ("12:55", "13:45"), 7: ("13:55", "14:45"), 8: ("15:00", "15:50"),
    9: ("15:55", "16:45"),
}

BTN_NOW = "⏱ Что сейчас?"
BTN_TODAY = "📅 Сегодня"
BTN_TOMORROW = "➡️ Завтра"
BTN_WEEK = "🗓 На неделю"
BTN_HW = "📚 Домашка"
BTN_ADD_HW = "➕ Добавить ДЗ"
BTN_EDIT = "✏️ Редактировать расписание"
MENU_BUTTONS = {BTN_NOW, BTN_TODAY, BTN_TOMORROW, BTN_WEEK, BTN_HW, BTN_ADD_HW, BTN_EDIT}

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_NOW)],
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
        [KeyboardButton(text=BTN_WEEK)],
        [KeyboardButton(text=BTN_HW), KeyboardButton(text=BTN_ADD_HW)],
        [KeyboardButton(text=BTN_EDIT)],
    ],
    resize_keyboard=True,
)


# ---------- хранение данных ----------

def save_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def load_schedule() -> dict:
    with open(SCHEDULE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    sched = data.setdefault("schedule", {})
    for key, name in zip(DAY_KEYS, DAY_NAMES):
        day = sched.setdefault(key, {"name": name, "lessons": []})
        day.setdefault("name", name)
        for l in day["lessons"]:
            l.setdefault("id", new_id())
    return data


DATA = load_schedule()
SCHEDULE = DATA["schedule"]


def save_schedule() -> None:
    for day in SCHEDULE.values():
        day["lessons"].sort(key=lambda l: (l["pair"], l["start"]))
    save_json(SCHEDULE_FILE, DATA)


def load_hw() -> dict:
    if HW_FILE.exists():
        with open(HW_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"next_id": 1, "items": []}


HW = load_hw()


def save_hw() -> None:
    save_json(HW_FILE, HW)


def pair_time(n: int) -> tuple[str, str]:
    t = DATA.get("pair_times", {}).get(str(n))
    if t:
        return t["start"], t["end"]
    return DEFAULT_PAIR_TIMES[n]


# ---------- время и расписание ----------

def now() -> datetime:
    return datetime.now(TZ)


def week_kind(d: date) -> str:
    start_monday = SEMESTER_START - timedelta(days=SEMESTER_START.weekday())
    d_monday = d - timedelta(days=d.weekday())
    return "numerator" if ((d_monday - start_monday).days // 7) % 2 == 0 else "denominator"


def lessons_for(d: date) -> list[dict]:
    day = SCHEDULE.get(DAY_KEYS[d.weekday()], {"lessons": []})
    kind = week_kind(d)
    res = [l for l in day["lessons"] if l.get("week", "every") in ("every", kind)]
    return sorted(res, key=lambda l: l["start"])


def dt(d: date, hhmm: str) -> datetime:
    return datetime.combine(d, time.fromisoformat(hhmm), tzinfo=TZ)


def merge(lessons: list[dict]) -> list[dict]:
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


def all_subjects() -> list[str]:
    return sorted({l["subject"] for day in SCHEDULE.values() for l in day["lessons"]})


# ---------- домашние задания ----------

def hw_for(d: date, subject: str) -> list[dict]:
    return [h for h in HW["items"] if h["due"] == d.isoformat() and h["subject"] == subject]


def lesson_start_on(d: date, subject: str) -> str | None:
    for l in merge(lessons_for(d)):
        if l["subject"] == subject:
            return l["start"]
    return None


def fmt_hw_list() -> tuple[str, "InlineKeyboardBuilder | None"]:
    items = sorted(HW["items"], key=lambda h: (h["due"], h["id"]))
    if not items:
        return "📚 Домашних заданий нет 🎉", None
    today = now().date()
    lines = ["📚 <b>Домашние задания</b>"]
    cur = None
    for n, h in enumerate(items, 1):
        d = date.fromisoformat(h["due"])
        if d != cur:
            cur = d
            label = f"{DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}"
            if d < today:
                label = "⚠️ Просрочено — " + label
            elif d == today:
                label += " (сегодня)"
            elif d == today + timedelta(days=1):
                label += " (завтра)"
            lines.append(f"\n📅 <b>{label}</b>")
        t = lesson_start_on(d, h["subject"])
        when = f" · пара в {t}" if t else ""
        lines.append(f"{n}. <b>{escape(h['subject'])}</b>{when}\n    {escape(h['text'])}")
    kb = InlineKeyboardBuilder()
    for n, h in enumerate(items, 1):
        kb.button(text=f"✅ {n}. {h['subject'][:28]}", callback_data=f"hx:{h['id']}")
    kb.adjust(1)
    lines.append("\nНажми на кнопку, когда сделаешь:")
    return "\n".join(lines), kb


# ---------- форматирование ----------

def fmt_lesson(l: dict, with_time: bool = True, d: date | None = None) -> str:
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
    if d:
        for h in hw_for(d, l["subject"]):
            lines.append(f"   📝 <i>ДЗ: {escape(h['text'])}</i>")
    return "\n".join(lines)


def fmt_day(d: date, title: str | None = None) -> str:
    title = title or f"{DAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}"
    lessons = merge(lessons_for(d))
    if not lessons:
        return f"📅 <b>{title}</b>\n\nПар нет 🎉"
    return f"📅 <b>{title}</b>\n\n" + "\n\n".join(fmt_lesson(l, d=d) for l in lessons)


def fmt_week(today: date) -> str:
    monday = today - timedelta(days=today.weekday())
    parts = []
    for i in range(6):
        d = monday + timedelta(days=i)
        mark = " 👈" if d == today else ""
        lessons = merge(lessons_for(d))
        header = f"<b>{DAY_NAMES[i]}</b>{mark}"
        if lessons:
            rows = []
            for l in lessons:
                hw = " 📝" if hw_for(d, l["subject"]) else ""
                rows.append(
                    f"• {escape(l['start'])}–{escape(l['end'])} {escape(l['subject'])} "
                    f"({escape(l['room'])}, {escape(l['type'])}){hw}"
                )
            parts.append(header + "\n" + "\n".join(rows))
        else:
            parts.append(header + "\n• пар нет")
    return "🗓 <b>Расписание на неделю</b>\n\n" + "\n\n".join(parts)


def find_next_lesson_after(moment: datetime):
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

    for l in lessons:
        start, end = dt(today, l["start"]), dt(today, l["end"])
        if start <= n < end:
            left = ceil_minutes(end - n)
            text = (
                f"📖 <b>Сейчас идёт пара</b>\n\n{fmt_lesson(l, with_time=False, d=today)}\n\n"
                f"⏳ До конца: <b>{fmt_min(left)}</b> (до {escape(l['end'])})"
            )
            nxt = next((x for x in lessons if x["start"] >= l["end"] and x is not l), None)
            if nxt and nxt["start"] != l["end"]:
                text += f"\n\nДальше: {escape(nxt['start'])} — {escape(nxt['subject'])}, {escape(nxt['room'])}"
            return text

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
            f"Следующая:\n{fmt_lesson(nl, with_time=False, d=nd)}"
        )

    head = "✅ <b>Пары на сегодня закончились</b>" if lessons else "🎉 <b>Сегодня пар нет</b>"
    when = "завтра" if nd == today + timedelta(days=1) else f"{DAY_NAMES[nd.weekday()].lower()}, {nd.strftime('%d.%m')}"
    return f"{head}\n\nСледующая пара — {when} в {escape(nl['start'])}:\n{fmt_lesson(nl, with_time=False, d=nd)}"


# ---------- вспомогательное для интерфейса ----------

class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if ALLOWED and (user is None or user.id not in ALLOWED):
            return None
        return await handler(event, data)


class AddLesson(StatesGroup):
    pair = State()
    subject = State()
    type = State()
    room = State()
    teacher = State()
    group = State()
    week = State()


class EditLesson(StatesGroup):
    value = State()


class AddHw(StatesGroup):
    subject = State()
    date = State()
    text = State()


async def send(target, text: str, kb=None):
    """Единая отправка: для кнопки — редактируем сообщение, для текста — новое сообщение."""
    markup = kb.as_markup() if hasattr(kb, "as_markup") else kb
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


def cancel_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data="ac")
    return kb


def choice_kb(prefix: str, options: list[str], per_row: int = 1) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for i, label in enumerate(options):
        kb.button(text=label, callback_data=f"{prefix}:{i}")
    kb.button(text="✖️ Отмена", callback_data="ac")
    kb.adjust(per_row, repeat=True)
    return kb


def find_lesson(lid: str):
    for i, key in enumerate(DAY_KEYS):
        for l in SCHEDULE[key]["lessons"]:
            if l.get("id") == lid:
                return i, l
    return None, None


def day_picker() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for i in range(6):
        kb.button(text=DAY_NAMES[i], callback_data=f"ed:{i}")
    kb.button(text="✖️ Закрыть", callback_data="ac")
    kb.adjust(2, repeat=True)
    return kb


def day_edit_view(i: int):
    lessons = sorted(SCHEDULE[DAY_KEYS[i]]["lessons"], key=lambda l: (l["pair"], l["start"]))
    kb = InlineKeyboardBuilder()
    rows = []
    for l in lessons:
        w = "" if l.get("week", "every") == "every" else f" [{'ч' if l['week'] == 'numerator' else 'з'}]"
        rows.append(f"{l['pair']} · {l['start']} {escape(l['subject'])} ({escape(l['room'])}){w}")
        kb.button(text=f"{l['pair']} · {l['subject'][:30]}", callback_data=f"el:{l['id']}")
    kb.button(text="➕ Добавить пару", callback_data=f"ea:{i}")
    kb.button(text="⬅️ К дням", callback_data="em")
    kb.adjust(1, repeat=True)
    body = "\n".join(rows) if rows else "Пар нет"
    return f"✏️ <b>{DAY_NAMES[i]}</b>\n\n{body}\n\nВыбери пару для правки или добавь новую:", kb


def lesson_card(l: dict, i: int):
    week = dict(WEEKS).get(l.get("week", "every"), "Каждая неделя")
    text = (
        f"✏️ <b>{escape(l['subject'])}</b>\n\n"
        f"№ пары: {l['pair']} ({l['start']}–{l['end']})\n"
        f"Тип: {escape(l['type'])}\n"
        f"Кабинет: {escape(l['room'])}\n"
        f"Преподаватель: {escape(l['teacher']) if l.get('teacher') else '—'}\n"
        f"Группа: {escape(l.get('group') or 'вся группа')}\n"
        f"Неделя: {week.lower()}\n"
        f"День: {DAY_NAMES[i]}\n\nЧто изменить?"
    )
    kb = InlineKeyboardBuilder()
    for f, label in FIELDS.items():
        kb.button(text=label, callback_data=f"ef:{l['id']}:{f}")
    kb.button(text="🗑 Удалить", callback_data=f"dl:{l['id']}")
    kb.button(text="⬅️ Назад", callback_data=f"ed:{i}")
    kb.adjust(2, 2, 2, 1, 2)
    return text, kb


def set_field(l: dict, field: str, raw: str) -> None:
    raw = raw.strip()
    if field == "type":
        l["type"] = TYPES[int(raw)]
    elif field == "week":
        l["week"] = WEEKS[int(raw)][0]
    elif field == "pair":
        n = int(raw)
        l["pair"] = n
        l["start"], l["end"] = pair_time(n)
    elif field == "teacher":
        l["teacher"] = None if raw in ("-", "") else raw
    elif field == "group":
        l["group"] = "вся группа" if raw == "all" or not raw else raw
    elif field == "subject":
        old = l["subject"]
        l["subject"] = raw
        for h in HW["items"]:  # переименовали предмет — домашки не теряем
            if h["subject"] == old:
                h["subject"] = raw
        save_hw()
    else:
        l[field] = raw


# ---------- хендлеры: меню и просмотр ----------

dp = Dispatcher(storage=MemoryStorage())
dp.update.outer_middleware(AccessMiddleware())


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Привет! Я бот с расписанием 📚\nВыбери действие в меню ниже.", reply_markup=MENU)


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Твой Telegram id: <code>{message.from_user.id}</code>")


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено ✅", reply_markup=MENU)


@dp.callback_query(F.data == "ac")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await send(cb, "Отменено ✅")


@dp.message(Command("now"))
@dp.message(F.text == BTN_NOW)
async def h_now(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(what_now(), reply_markup=MENU)


@dp.message(Command("today"))
@dp.message(F.text == BTN_TODAY)
async def h_today(message: Message, state: FSMContext):
    await state.clear()
    n = now()
    await message.answer(fmt_day(n.date(), f"Сегодня, {DAY_NAMES[n.weekday()]}"), reply_markup=MENU)


@dp.message(Command("tomorrow"))
@dp.message(F.text == BTN_TOMORROW)
async def h_tomorrow(message: Message, state: FSMContext):
    await state.clear()
    d = now().date() + timedelta(days=1)
    await message.answer(fmt_day(d, f"Завтра, {DAY_NAMES[d.weekday()]}"), reply_markup=MENU)


@dp.message(Command("week"))
@dp.message(F.text == BTN_WEEK)
async def h_week(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(fmt_week(now().date()), reply_markup=MENU)


# ---------- домашние задания ----------

@dp.message(Command("hw"))
@dp.message(F.text == BTN_HW)
async def h_hw(message: Message, state: FSMContext):
    await state.clear()
    text, kb = fmt_hw_list()
    await message.answer(text, reply_markup=kb.as_markup() if kb else MENU)


@dp.callback_query(F.data.startswith("hx:"))
async def cb_hw_done(cb: CallbackQuery):
    hid = int(cb.data.split(":")[1])
    HW["items"] = [h for h in HW["items"] if h["id"] != hid]
    save_hw()
    text, kb = fmt_hw_list()
    await send(cb, text, kb)


@dp.message(Command("addhw"))
@dp.message(F.text == BTN_ADD_HW)
async def h_add_hw(message: Message, state: FSMContext):
    await state.clear()
    subjects = all_subjects()
    if not subjects:
        await message.answer("В расписании нет предметов. Сначала добавь пары.", reply_markup=MENU)
        return
    await state.set_state(AddHw.subject)
    await state.update_data(subjects=subjects)
    await message.answer("➕ <b>Новое ДЗ</b>\n\nПо какому предмету?", reply_markup=choice_kb("hs", subjects).as_markup())


@dp.callback_query(AddHw.subject, F.data.startswith("hs:"))
async def cb_hw_subject(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data["subjects"][int(cb.data.split(":")[1])]
    today, n = now().date(), now()
    options = []
    for add in range(0, 28):
        d = today + timedelta(days=add)
        for l in merge(lessons_for(d)):
            if l["subject"] == subject and not (add == 0 and dt(d, l["end"]) <= n):
                options.append((d, l["start"]))
                break
        if len(options) >= 6:
            break
    if not options:
        await send(cb, "Не нашёл ближайших пар по этому предмету.")
        await state.clear()
        return
    await state.update_data(subject=subject)
    await state.set_state(AddHw.date)
    kb = InlineKeyboardBuilder()
    for d, start in options:
        kb.button(text=f"{SHORT[d.weekday()]} {d.strftime('%d.%m')} · {start}", callback_data=f"hd:{d.isoformat()}")
    kb.button(text="✖️ Отмена", callback_data="ac")
    kb.adjust(2, repeat=True)
    await send(cb, f"<b>{escape(subject)}</b>\n\nК какой паре задание?", kb)


@dp.callback_query(AddHw.date, F.data.startswith("hd:"))
async def cb_hw_date(cb: CallbackQuery, state: FSMContext):
    await state.update_data(due=cb.data.split(":", 1)[1])
    await state.set_state(AddHw.text)
    await send(cb, "Напиши, что нужно сделать (одним сообщением):", cancel_kb())


@dp.message(AddHw.text, F.text)
async def h_hw_text(message: Message, state: FSMContext):
    data = await state.get_data()
    HW["items"].append({
        "id": HW["next_id"], "subject": data["subject"], "due": data["due"], "text": message.text.strip(),
    })
    HW["next_id"] += 1
    save_hw()
    await state.clear()
    d = date.fromisoformat(data["due"])
    await message.answer(
        f"✅ Записал: <b>{escape(data['subject'])}</b> к паре {DAY_NAMES[d.weekday()].lower()}, {d.strftime('%d.%m')}.",
        reply_markup=MENU,
    )


# ---------- редактирование расписания ----------

@dp.message(Command("edit"))
@dp.message(F.text == BTN_EDIT)
async def h_edit(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✏️ Какой день редактируем?", reply_markup=day_picker().as_markup())


@dp.callback_query(F.data == "em")
async def cb_edit_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await send(cb, "✏️ Какой день редактируем?", day_picker())


@dp.callback_query(F.data.startswith("ed:"))
async def cb_day(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = day_edit_view(int(cb.data.split(":")[1]))
    await send(cb, text, kb)


@dp.callback_query(F.data.startswith("el:"))
async def cb_lesson(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    i, l = find_lesson(cb.data.split(":")[1])
    if not l:
        await cb.answer("Пара не найдена", show_alert=True)
        return
    text, kb = lesson_card(l, i)
    await send(cb, text, kb)


@dp.callback_query(F.data.startswith("ef:"))
async def cb_field(cb: CallbackQuery, state: FSMContext):
    _, lid, field = cb.data.split(":")
    i, l = find_lesson(lid)
    if not l:
        await cb.answer("Пара не найдена", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    if field in ("type", "week", "pair"):
        if field == "type":
            opts = [(str(k), v.capitalize()) for k, v in enumerate(TYPES)]
        elif field == "week":
            opts = [(str(k), w[1]) for k, w in enumerate(WEEKS)]
        else:
            opts = [(str(n), f"{n} · {pair_time(n)[0]}") for n in range(1, 10)]
        for v, label in opts:
            kb.button(text=label, callback_data=f"ev:{lid}:{field}:{v}")
        kb.button(text="⬅️ Назад", callback_data=f"el:{lid}")
        kb.adjust(3 if field == "pair" else 1, repeat=True)
        await send(cb, f"{FIELDS[field]} — выбери вариант:", kb)
        return
    await state.set_state(EditLesson.value)
    await state.update_data(lid=lid, field=field)
    hint = {"teacher": "\n(Напиши «-», чтобы убрать преподавателя)", "group": ""}.get(field, "")
    if field == "group":
        kb.button(text="Вся группа", callback_data=f"ev:{lid}:group:all")
    kb.button(text="⬅️ Назад", callback_data=f"el:{lid}")
    kb.adjust(1)
    await send(cb, f"Новое значение для «{FIELDS[field]}» — напиши текстом.{hint}", kb)


@dp.callback_query(F.data.startswith("ev:"))
async def cb_value(cb: CallbackQuery, state: FSMContext):
    _, lid, field, val = cb.data.split(":", 3)
    await state.clear()
    i, l = find_lesson(lid)
    if not l:
        await cb.answer("Пара не найдена", show_alert=True)
        return
    set_field(l, field, val)
    save_schedule()
    text, kb = lesson_card(l, i)
    await send(cb, "✅ Сохранено\n\n" + text, kb)


@dp.message(EditLesson.value, F.text)
async def h_edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    i, l = find_lesson(data["lid"])
    if not l:
        await message.answer("Пара не найдена.", reply_markup=MENU)
        return
    set_field(l, data["field"], message.text)
    save_schedule()
    text, kb = lesson_card(l, i)
    await message.answer("✅ Сохранено\n\n" + text, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("dl:"))
async def cb_delete_ask(cb: CallbackQuery):
    lid = cb.data.split(":")[1]
    i, l = find_lesson(lid)
    if not l:
        await cb.answer("Пара не найдена", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=f"dy:{lid}")
    kb.button(text="Нет", callback_data=f"el:{lid}")
    await send(cb, f"Удалить пару «{escape(l['subject'])}» ({DAY_NAMES[i].lower()}, {l['start']})?", kb)


@dp.callback_query(F.data.startswith("dy:"))
async def cb_delete(cb: CallbackQuery):
    i, l = find_lesson(cb.data.split(":")[1])
    if l:
        SCHEDULE[DAY_KEYS[i]]["lessons"].remove(l)
        save_schedule()
        text, kb = day_edit_view(i)
        await send(cb, "🗑 Удалено\n\n" + text, kb)
    else:
        await cb.answer("Пара не найдена", show_alert=True)


# --- добавление новой пары ---

@dp.callback_query(F.data.startswith("ea:"))
async def cb_add_start(cb: CallbackQuery, state: FSMContext):
    i = int(cb.data.split(":")[1])
    await state.clear()
    await state.set_state(AddLesson.pair)
    await state.update_data(day=i)
    kb = InlineKeyboardBuilder()
    for n in range(1, 10):
        kb.button(text=f"{n} · {pair_time(n)[0]}", callback_data=f"ap:{n}")
    kb.button(text="✖️ Отмена", callback_data="ac")
    kb.adjust(3, repeat=True)
    await send(cb, f"➕ <b>Новая пара — {DAY_NAMES[i]}</b>\n\nВыбери номер пары:", kb)


@dp.callback_query(AddLesson.pair, F.data.startswith("ap:"))
async def cb_add_pair(cb: CallbackQuery, state: FSMContext):
    await state.update_data(pair=int(cb.data.split(":")[1]))
    await state.set_state(AddLesson.subject)
    await send(cb, "Название предмета? (напиши текстом)", cancel_kb())


@dp.message(AddLesson.subject, F.text)
async def add_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await state.set_state(AddLesson.type)
    await send(message, "Тип занятия:", choice_kb("at", [t.capitalize() for t in TYPES]))


@dp.callback_query(AddLesson.type, F.data.startswith("at:"))
async def cb_add_type(cb: CallbackQuery, state: FSMContext):
    await state.update_data(type=TYPES[int(cb.data.split(":")[1])])
    await state.set_state(AddLesson.room)
    await send(cb, "Кабинет? (например, Г-1-301)", cancel_kb())


@dp.message(AddLesson.room, F.text)
async def add_room(message: Message, state: FSMContext):
    await state.update_data(room=message.text.strip())
    await state.set_state(AddLesson.teacher)
    kb = InlineKeyboardBuilder()
    kb.button(text="Без преподавателя", callback_data="as")
    kb.button(text="✖️ Отмена", callback_data="ac")
    kb.adjust(1)
    await send(message, "Преподаватель? (текстом или кнопкой)", kb)


async def ask_group(target, state: FSMContext):
    await state.set_state(AddLesson.group)
    kb = InlineKeyboardBuilder()
    kb.button(text="Вся группа", callback_data="ag")
    kb.button(text="✖️ Отмена", callback_data="ac")
    kb.adjust(1)
    await send(target, "Для кого пара? (напиши, например, «5-я подгруппа», или нажми кнопку)", kb)


@dp.callback_query(AddLesson.teacher, F.data == "as")
async def cb_add_no_teacher(cb: CallbackQuery, state: FSMContext):
    await state.update_data(teacher=None)
    await ask_group(cb, state)


@dp.message(AddLesson.teacher, F.text)
async def add_teacher(message: Message, state: FSMContext):
    await state.update_data(teacher=message.text.strip())
    await ask_group(message, state)


async def ask_week(target, state: FSMContext):
    await state.set_state(AddLesson.week)
    await send(target, "Как часто проходит пара?", choice_kb("aw", [w[1] for w in WEEKS]))


@dp.callback_query(AddLesson.group, F.data == "ag")
async def cb_add_all_group(cb: CallbackQuery, state: FSMContext):
    await state.update_data(group="вся группа")
    await ask_week(cb, state)


@dp.message(AddLesson.group, F.text)
async def add_group(message: Message, state: FSMContext):
    await state.update_data(group=message.text.strip())
    await ask_week(message, state)


@dp.callback_query(AddLesson.week, F.data.startswith("aw:"))
async def cb_add_week(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    start, end = pair_time(data["pair"])
    SCHEDULE[DAY_KEYS[data["day"]]]["lessons"].append({
        "id": new_id(), "pair": data["pair"], "start": start, "end": end,
        "subject": data["subject"], "type": data["type"], "room": data["room"],
        "teacher": data.get("teacher"), "teacher_title": None, "group": data["group"],
        "week": WEEKS[int(cb.data.split(":")[1])][0],
    })
    save_schedule()
    text, kb = day_edit_view(data["day"])
    await send(cb, "✅ Пара добавлена\n\n" + text, kb)


# ---------- запасной обработчик (должен быть последним) ----------

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
