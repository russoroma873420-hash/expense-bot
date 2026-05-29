import logging
import os
import re
import sqlite3
import csv
import io
import html
import base64
import asyncio
from datetime import datetime, date, timedelta, time as dt_time
import pytz
import requests
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters, JobQueue

# Распознавание чеков через polza.ai (российский агрегатор, оплата в рублях, работает из РФ).
# polza.ai — OpenAI-совместимый API. Ключ берётся из переменной окружения POLZA_API_KEY.
LLM_BASE_URL = "https://api.polza.ai/api/v1"
LLM_API_URL = f"{LLM_BASE_URL}/chat/completions"
LLM_API_KEY_ENV = "POLZA_API_KEY"
# Перебираем модели по порядку: берём первую, которая ответит и умеет читать картинки.
# Дешёвые vision-модели идут первыми. Точные ID можно посмотреть командой /models.
LLM_MODELS = [
    "qwen/qwen3-vl-8b-instruct",
    "qwen/qwen3-vl-32b-instruct",
    "amazon/nova-lite-v1",
    "google/gemini-3.5-flash",
]
RECEIPT_PROMPT = (
    "Посмотри на этот чек и извлеки основную информацию. "
    "Ответь строго в формате: НАЗВАНИЕ|СУММА (только число, без валюты). "
    "Например: кофе эспрессо|450 или пицца маргарита|650. "
    "Если на чеке несколько товаров, выбери самый дорогой или итоговую сумму. "
    "Если не можешь распознать чек, ответь только: ОШИБКА"
)


def _llm_chat(content, api_key: str):
    """Перебирает LLM_MODELS, возвращает (текст_ответа, имя_модели).
    content — это значение поля message.content (строка или список частей)."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error = None
    for model in LLM_MODELS:
        payload = {"model": model, "messages": [{"role": "user", "content": content}]}
        try:
            resp = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=60)
            if resp.status_code in (400, 404, 429, 502, 503):
                # модель недоступна/занята/не умеет картинки — пробуем следующую
                last_error = f"{model}: HTTP {resp.status_code} {resp.text[:120]}"
                continue
            resp.raise_for_status()
            data = resp.json()
            # некоторые провайдеры возвращают ошибку в теле с кодом 200
            if "choices" not in data:
                last_error = f"{model}: {str(data)[:200]}"
                continue
            return data["choices"][0]["message"]["content"].strip(), model
        except Exception as e:
            last_error = f"{model}: {type(e).__name__}: {e}"
            continue
    raise RuntimeError(f"Все модели недоступны. Последняя ошибка — {last_error}")


def _llm_vision(photo_bytes: bytes, prompt: str, api_key: str) -> str:
    """Синхронный запрос с картинкой. Возвращает текст ответа модели."""
    photo_b64 = base64.standard_b64encode(bytes(photo_bytes)).decode("utf-8")
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{photo_b64}"}},
    ]
    text, _model = _llm_chat(content, api_key)
    return text


def _llm_list_models(api_key: str) -> list:
    """Запрашивает список моделей у polza.ai (/models). Возвращает список словарей."""
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.get(f"{LLM_BASE_URL}/models", headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", data if isinstance(data, list) else [])

# Настройка логов
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = "expenses.db"
CATEGORIES = [
    "Еда",
    "Транспорт",
    "Кафе",
    "Продукты",
    "Одежда",
    "Развлечения",
    "Здоровье",
    "Коммуналка",
    "Подарки",
    "Другое",
]

# Разрешенные пользователи (владелец и жена)
ALLOWED_USER_IDS = [652328822, 970623315]  # 652328822 - ты, 970623315 - Катя

# Семейный чат для уведомлений. Установи None или ID группы.
# Как получить chat_id группы: добавь бота в группу, отправь любое сообщение, посмотри логи: "chat_id: -XXXXX"
FAMILY_CHAT_ID = None

# Timezone для расписания работ
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Словарь имен пользователей для отображения
USER_NAMES = {
    652328822: "Александр",
    970623315: "Екатерина",
}

EXPENSE_RE = re.compile(r"^(.+?)\s+(\d+(?:[.,]\d+)?)$")


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            item TEXT,
            amount REAL,
            category TEXT,
            created_at TEXT
        )
        """
    )
    cursor.execute("PRAGMA table_info(expenses)")
    columns = [row[1] for row in cursor.fetchall()]
    if "user_name" not in columns:
        cursor.execute("ALTER TABLE expenses ADD COLUMN user_name TEXT")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE
        )
        """
    )

    # Миграция старых записей: если в created_at только дата, дополняем временем 00:00:00
    cursor.execute("SELECT id, created_at FROM expenses")
    rows = cursor.fetchall()
    for expense_id, created_at in rows:
        if created_at and re.match(r"^\d{4}-\d{2}-\d{2}$", created_at):
            cursor.execute(
                "UPDATE expenses SET created_at = ? WHERE id = ?",
                (f"{created_at}T00:00:00", expense_id),
            )

    # Таблица для настроек (лимиты, chat_id и т.д.)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    # Таблица для регулярных расходов
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS regular_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            amount REAL,
            created_at TEXT
        )
        """
    )

    # Таблица для целей накопления
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            target_amount REAL,
            current_amount REAL,
            created_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def get_custom_categories() -> list:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM custom_categories ORDER BY id")
    rows = cursor.fetchall()
    conn.close()
    return rows


def add_custom_category(name: str) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO custom_categories (name) VALUES (?)", (name,))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False


def delete_custom_category(cat_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM custom_categories WHERE id = ?", (cat_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_setting(key: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


def set_setting(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def add_regular_expense(name: str, amount: float) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO regular_expenses (name, amount, created_at) VALUES (?, ?, ?)",
        (name, amount, datetime.utcnow().isoformat()),
    )
    expense_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return expense_id


def get_regular_expenses() -> list:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, amount FROM regular_expenses ORDER BY id")
    expenses = cursor.fetchall()
    conn.close()
    return expenses


def delete_regular_expense(expense_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM regular_expenses WHERE id = ?", (expense_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def add_goal(name: str, target_amount: float) -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO goals (name, target_amount, current_amount, created_at) VALUES (?, ?, ?, ?)",
            (name, target_amount, 0, datetime.utcnow().isoformat()),
        )
        goal_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return goal_id
    except sqlite3.IntegrityError:
        return -1


def get_goals() -> list:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, target_amount, current_amount FROM goals ORDER BY id")
    goals = cursor.fetchall()
    conn.close()
    return goals


def get_goal_by_name(name: str) -> tuple:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, target_amount, current_amount FROM goals WHERE name = ?", (name,))
    result = cursor.fetchone()
    conn.close()
    return result


def update_goal(goal_id: int, current_amount: float) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE goals SET current_amount = ? WHERE id = ?", (current_amount, goal_id))
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success


def delete_goal(goal_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_all_categories() -> list:
    custom = [name for _, name in get_custom_categories()]
    return CATEGORIES + custom


def add_expense(user_id: int, user_name: str, item: str, amount: float, category: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO expenses (user_id, user_name, item, amount, category, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, user_name, item, amount, category, datetime.utcnow().isoformat()),
    )
    expense_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return expense_id


def delete_expense(expense_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def update_expense(expense_id: int, **kwargs) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    allowed_fields = ["item", "amount"]
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
    if not updates:
        conn.close()
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [expense_id]
    cursor.execute(f"UPDATE expenses SET {set_clause} WHERE id = ?", values)
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success


def get_recent_expenses(limit: int = 10) -> list:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_name, item, amount, category, created_at FROM expenses ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    expenses = cursor.fetchall()
    conn.close()
    return expenses


def get_monthly_report(year: int, month: int) -> tuple:
    # Получаем первый и последний день месяца
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = date(year, month + 1, 1) - timedelta(days=1)
    
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Общая сумма
    cursor.execute(
        "SELECT SUM(amount) FROM expenses WHERE date(created_at) BETWEEN ? AND ?",
        (start_str, end_str),
    )
    total = cursor.fetchone()[0] or 0
    
    # По пользователям
    cursor.execute(
        "SELECT user_id, SUM(amount) FROM expenses WHERE date(created_at) BETWEEN ? AND ? GROUP BY user_id ORDER BY SUM(amount) DESC",
        (start_str, end_str),
    )
    by_user_raw = cursor.fetchall()
    by_user = [(USER_NAMES.get(user_id, f"ID:{user_id}"), amount) for user_id, amount in by_user_raw]
    
    # Хронология
    cursor.execute(
        "SELECT user_name, item, amount, category, created_at FROM expenses WHERE date(created_at) BETWEEN ? AND ? ORDER BY created_at DESC",
        (start_str, end_str),
    )
    timeline = cursor.fetchall()
    
    conn.close()
    
    return total, by_user, timeline


def format_report(total: float, by_user: list, timeline: list, month_name: str) -> str:
    report = f"📊 <b>Отчет за {month_name}</b>\n\n"
    report += f"💰 <b>Общие траты:</b> {total:.2f} ₽\n\n"
    
    report += "👥 <b>По членам семьи:</b>\n"
    if by_user:
        for user_name, amount in by_user:
            report += f"  • {html.escape(user_name)}: {amount:.2f} ₽\n"
    else:
        report += "  (нет данных)\n"
    
    report += "\n📅 <b>Хронология:</b>\n"
    if timeline:
        for user_name, item, amount, category, created_at in timeline:
            dt = datetime.fromisoformat(created_at)
            time_str = dt.strftime("%d.%m %H:%M")
            report += f"  {time_str} {html.escape(item)} — {amount:.2f} ₽ ({html.escape(category)}) | {html.escape(user_name)}\n"
    else:
        report += "  (нет данных)\n"
    
    return report


def create_csv_report(total: float, by_user: list, timeline: list, month_name: str) -> io.StringIO:
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    
    writer.writerow([f"Отчет за {month_name}"])
    writer.writerow([])
    writer.writerow(["Общие траты", f"{total:.2f} руб."])
    writer.writerow([])
    writer.writerow(["По членам семьи"])
    for user_name, amount in by_user:
        writer.writerow([user_name, f"{amount:.2f} руб."])
    writer.writerow([])
    writer.writerow(["Хронология"])
    writer.writerow(["Дата", "Время", "Наименование", "Сумма", "Категория", "Кто добавил"])
    for user_name, item, amount, category, created_at in timeline:
        dt = datetime.fromisoformat(created_at)
        date_str = dt.strftime("%d.%m.%Y")
        time_str = dt.strftime("%H:%M")
        writer.writerow([date_str, time_str, item, f"{amount:.2f}", category, user_name])
    
    output.seek(0)
    return output


def get_monthly_stats() -> dict:
    from datetime import date
    current_month = date.today().strftime("%Y-%m")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Общая сумма
    cursor.execute(
        "SELECT SUM(amount) FROM expenses WHERE created_at LIKE ?",
        (f"{current_month}%",),
    )
    total = cursor.fetchone()[0] or 0
    
    # По пользователям
    cursor.execute(
        "SELECT user_id, SUM(amount) FROM expenses WHERE created_at LIKE ? GROUP BY user_id ORDER BY SUM(amount) DESC",
        (f"{current_month}%",),
    )
    by_user_raw = cursor.fetchall()
    by_user = [(USER_NAMES.get(user_id, f"ID:{user_id}"), amount) for user_id, amount in by_user_raw]
    
    # Последние 3 операции за текущий месяц
    cursor.execute(
        "SELECT user_name, item, amount, category, created_at FROM expenses WHERE created_at LIKE ? ORDER BY created_at DESC LIMIT 3",
        (f"{current_month}%",),
    )
    last_three = cursor.fetchall()
    
    conn.close()
    
    return {
        "total": total,
        "by_user": by_user,
        "last_three": last_three,
    }


def build_categories_keyboard() -> InlineKeyboardMarkup:
    all_cats = get_all_categories()
    buttons = [InlineKeyboardButton(text=cat, callback_data=f"cat_{cat}") for cat in all_cats]
    keyboard = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    keyboard.append([InlineKeyboardButton("➕ Новая категория", callback_data="new_category")])
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("❓ Справка", callback_data="menu_help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Я помогу вести семейный бюджет.\n\n"
        "Отправь расход в формате: <b>название сумма</b>\n"
        "Например: <b>кофе 300</b>\n\n"
        "После этого выбери категорию, и я сохраню запись.",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Используй сообщение в формате 'пицца 450' или 'такси 120'.\n"
        "Затем выбери категорию."
    )


async def diag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Самодиагностика: проверяем доступ к polza.ai прямо из чата, без логов сервера
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    lines = ["🔍 <b>Диагностика</b>\n"]

    # 1. Виден ли ключ polza.ai боту
    api_key = os.environ.get(LLM_API_KEY_ENV)
    if api_key:
        lines.append(f"✅ {LLM_API_KEY_ENV} виден (…{html.escape(api_key[-4:])})")
    else:
        lines.append(f"❌ {LLM_API_KEY_ENV} НЕ виден боту")

    lines.append(f"ℹ️ Моделей в очереди: {len(LLM_MODELS)}")

    # 2. Пробный запрос — перебираем модели, показываем какая ответила
    if api_key:
        try:
            answer, used_model = await asyncio.to_thread(
                _llm_chat, "Ответь одним словом: работает", api_key
            )
            lines.append(f"✅ Отвечает модель: {html.escape(used_model)}")
            lines.append(f"   Ответ: {html.escape(answer[:50])}")
        except Exception as e:
            lines.append(f"❌ Ни одна модель не ответила:\n<code>{html.escape(str(e)[:500])}</code>")
            lines.append("\nПодсказка: нажми /models — покажу точные ID доступных моделей.")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Показывает реальные ID моделей от polza.ai, чтобы не угадывать имена
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    api_key = os.environ.get(LLM_API_KEY_ENV)
    if not api_key:
        await update.message.reply_text(f"❌ Переменная {LLM_API_KEY_ENV} не установлена")
        return

    try:
        models = await asyncio.to_thread(_llm_list_models, api_key)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не удалось получить список моделей:\n<code>{html.escape(str(e)[:400])}</code>",
            parse_mode="HTML",
        )
        return

    # Вытаскиваем id и отбираем те, что похожи на vision-модели
    ids = [m.get("id", "") for m in models if isinstance(m, dict) and m.get("id")]
    keywords = ("vl", "vision", "claude", "gpt-4o", "nova", "gemini", "qwen", "pixtral", "llama-3.2")
    vision_ids = [i for i in ids if any(k in i.lower() for k in keywords)]

    if vision_ids:
        text = "🖼 <b>Модели с распознаванием картинок</b> (всего моделей: {}):\n\n".format(len(ids))
        text += "\n".join(f"<code>{html.escape(i)}</code>" for i in vision_ids[:60])
    elif ids:
        text = "📋 <b>Доступные модели</b> (первые 60 из {}):\n\n".format(len(ids))
        text += "\n".join(f"<code>{html.escape(i)}</code>" for i in ids[:60])
    else:
        text = "Список моделей пуст или формат ответа неожиданный."

    # Telegram ограничивает длину сообщения ~4096 символов
    await update.message.reply_text(text[:4000], parse_mode="HTML")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    stats = get_monthly_stats()
    month_name = date.today().strftime("%B %Y")
    report = f"📊 <b>Отчет за {month_name}</b>\n\n"
    report += f"💰 <b>Общие траты:</b> {stats['total']:.2f} ₽\n\n"
    report += "👥 <b>По членам семьи:</b>\n"
    if stats['by_user']:
        for user_name, amount in stats['by_user']:
            report += f"  • {user_name}: {amount:.2f} ₽\n"
    else:
        report += "  (нет данных)\n"
    report += "\n"
    report += "⏱️ <b>Последние 3 операции:</b>\n"
    if stats['last_three']:
        for user_name, item, amount, category, created_at in stats['last_three']:
            dt = datetime.fromisoformat(created_at)
            time_str = dt.strftime("%d.%m %H:%M")
            report += f"  {time_str} {html.escape(item)} — {amount:.2f} ₽ ({html.escape(category)}) | {html.escape(user_name)}\n"
    else:
        report += "  (нет данных)\n"

    await update.message.reply_text(report, parse_mode="HTML")


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    expenses = get_recent_expenses(10)
    if not expenses:
        await update.message.reply_text("📝 Нет сохраненных расходов")
        return

    text = "📝 <b>Последние расходы:</b>\n\n"
    keyboard = []

    for expense_id, user_name, item, amount, category, created_at in expenses:
        dt = datetime.fromisoformat(created_at)
        time_str = dt.strftime("%d.%m %H:%M")
        text += f"• {time_str} {html.escape(item)} — {amount:.2f} ₽ ({html.escape(category)}) | {html.escape(user_name)}\n"
        keyboard.append([
            InlineKeyboardButton("✏️", callback_data=f"edit_expense_{expense_id}"),
            InlineKeyboardButton("❌", callback_data=f"delete_{expense_id}"),
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    keyboard = [
        [InlineKeyboardButton("📅 Текущий месяц", callback_data="report_current")],
        [InlineKeyboardButton("📅 Прошлый месяц", callback_data="report_previous")],
        [InlineKeyboardButton("📊 График", callback_data="chart_current")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📊 Выберите период для отчета:",
        reply_markup=reply_markup
    )


async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    custom = get_custom_categories()
    if not custom:
        await update.message.reply_text("Своих категорий пока нет. Добавь через кнопку «➕ Новая категория» при вводе расхода.")
        return

    keyboard = [
        [InlineKeyboardButton(f"❌ {name}", callback_data=f"delcat_{cid}")]
        for cid, name in custom
    ]
    await update.message.reply_text(
        "🗂 <b>Свои категории</b>\nНажми на категорию, чтобы удалить:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def setlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Использование: /setlimit 50000")
        return

    try:
        limit = float(context.args[0])
        if limit <= 0:
            await update.message.reply_text("Лимит должен быть положительным числом")
            return
        set_setting("monthly_limit", str(limit))
        await update.message.reply_text(f"✅ Месячный лимит установлен: {limit:.2f} ₽")
    except ValueError:
        await update.message.reply_text("Ошибка: введи число, например 50000 или 50000.50")


async def regular_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    regular = get_regular_expenses()
    keyboard = []

    for exp_id, name, amount in regular:
        keyboard.append([
            InlineKeyboardButton(f"➕ {name} ({amount:.2f}₽)", callback_data=f"add_regular_{exp_id}"),
            InlineKeyboardButton("❌", callback_data=f"del_regular_{exp_id}")
        ])

    keyboard.append([InlineKeyboardButton("➕ Добавить регулярный", callback_data="new_regular")])

    if regular:
        await update.message.reply_text(
            "📋 <b>Регулярные расходы:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await update.message.reply_text(
            "📋 Регулярных расходов нет. Добавь новый:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def goals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    goals = get_goals()
    keyboard = []
    text = "🎯 <b>Цели накопления:</b>\n\n"

    for goal_id, name, target, current in goals:
        percentage = (current / target * 100) if target > 0 else 0
        filled = int(percentage / 10)
        bar = "█" * filled + "░" * (10 - filled)
        text += f"{bar} {current:.0f}/{target:.0f} ₽ ({percentage:.0f}%)\n"
        text += f"  {html.escape(name)}\n\n"
        keyboard.append([
            InlineKeyboardButton(f"➕ Пополнить", callback_data=f"contrib_goal_{goal_id}"),
            InlineKeyboardButton("❌", callback_data=f"del_goal_{goal_id}")
        ])

    keyboard.append([InlineKeyboardButton("➕ Добавить цель", callback_data="new_goal")])

    if goals:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await update.message.reply_text(
            "🎯 Целей накопления нет. Добавь новую:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def addgoal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /addgoal Название 100000")
        return

    try:
        name = " ".join(context.args[:-1])
        target = float(context.args[-1])
        if target <= 0:
            await update.message.reply_text("Сумма должна быть положительной")
            return
        goal_id = add_goal(name, target)
        if goal_id == -1:
            await update.message.reply_text(f"Цель '{html.escape(name)}' уже существует", parse_mode="HTML")
        else:
            await update.message.reply_text(f"✅ Цель '{html.escape(name)}' создана на {target:.2f} ₽", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("Ошибка: введи число для суммы, например: /addgoal Отпуск 150000")


async def contribute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /contribute НазваниеЦели 5000")
        return

    try:
        goal_name = " ".join(context.args[:-1])
        amount = float(context.args[-1])
        if amount <= 0:
            await update.message.reply_text("Сумма должна быть положительной")
            return

        goal = get_goal_by_name(goal_name)
        if not goal:
            await update.message.reply_text(f"Цель '{html.escape(goal_name)}' не найдена", parse_mode="HTML")
            return

        goal_id, _, target, current = goal
        new_current = current + amount
        if update_goal(goal_id, new_current):
            percentage = (new_current / target * 100) if target > 0 else 0
            filled = int(percentage / 10)
            bar = "█" * filled + "░" * (10 - filled)
            await update.message.reply_text(
                f"✅ Пополнено!\n\n🎯 {html.escape(goal_name)}\n{bar} {new_current:.0f}/{target:.0f} ₽ ({percentage:.0f}%)",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("Ошибка при пополнении цели")
    except ValueError:
        await update.message.reply_text("Ошибка: введи число для суммы, например: /contribute Отпуск 5000")


async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    today = date.today()
    keyboard = [
        [InlineKeyboardButton("📅 Этот месяц", callback_data="chart_current")],
        [InlineKeyboardButton("📅 Прошлый месяц", callback_data="chart_previous")],
        [InlineKeyboardButton("📝 Свой диапазон", callback_data="chart_custom")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📊 Выберите период для графика:",
        reply_markup=reply_markup
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    await update.message.reply_text("⏳ Анализирую чек...")

    try:
        file = await update.message.photo[-1].get_file()
        photo_bytes = await file.download_as_bytearray()

        api_key = os.environ.get(LLM_API_KEY_ENV)
        if not api_key:
            await update.message.reply_text(f"❌ Ошибка: переменная {LLM_API_KEY_ENV} не установлена")
            return

        # Синхронный HTTP-запрос выносим в поток, чтобы не блокировать бота
        response_text = await asyncio.to_thread(
            _llm_vision, photo_bytes, RECEIPT_PROMPT, api_key
        )

        if "ОШИБКА" in response_text.upper():
            await update.message.reply_text(
                "❌ Не удалось распознать чек. Пожалуйста, введите расход в формате: название сумма\nНапример: кофе 300"
            )
            return

        if "|" not in response_text:
            await update.message.reply_text(
                "❌ Не удалось распознать чек. Пожалуйста, введите расход в формате: название сумма\nНапример: кофе 300"
            )
            return

        item, amount_str = response_text.split("|", 1)
        item = item.strip()
        amount_str = amount_str.strip().replace(",", ".")

        try:
            amount = float(amount_str)
            if amount <= 0:
                raise ValueError("Сумма должна быть положительной")
        except ValueError:
            await update.message.reply_text(
                "❌ Не удалось распознать сумму на чеке. Пожалуйста, введите расход в формате: название сумма\nНапример: кофе 300"
            )
            return

        context.user_data["pending_expense"] = {"item": item, "amount": amount}
        await update.message.reply_text(
            f"Запись: {item} — {amount:.2f}\nВыберите категорию:",
            reply_markup=build_categories_keyboard(),
        )

    except Exception as e:
        logger.error(f"Ошибка при обработке фото: {e}")
        # Показываем саму причину прямо в чат, чтобы не лазить в логи сервера
        await update.message.reply_text(
            f"❌ Ошибка при обработке чека:\n<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>\n\n"
            "Можно ввести расход вручную в формате: название сумма",
            parse_mode="HTML",
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

    text = update.message.text.strip()

    # Ввод названия новой категории
    if context.user_data.get("adding_category"):
        context.user_data.pop("adding_category")
        name = text.strip()
        if not name:
            await update.message.reply_text("Название не может быть пустым.")
            return
        if add_custom_category(name):
            await update.message.reply_text(f"✅ Категория «{html.escape(name)}» добавлена.")
        else:
            await update.message.reply_text(f"Категория «{html.escape(name)}» уже существует.")
        # Если есть незавершённый расход — показываем обновлённую клавиатуру
        if context.user_data.get("pending_expense"):
            pending = context.user_data["pending_expense"]
            await update.message.reply_text(
                f"Запись: {pending['item']} — {pending['amount']:.2f}\nВыберите категорию:",
                reply_markup=build_categories_keyboard(),
            )
        return

    # Добавление регулярного расхода
    if context.user_data.get("adding_regular"):
        context.user_data.pop("adding_regular")
        match = EXPENSE_RE.match(text)
        if not match:
            await update.message.reply_text(
                "Неверный формат. Используй: название сумма\nНапример: интернет 1500"
            )
            return
        name = match.group(1).strip()
        try:
            amount = float(match.group(2).replace(",", "."))
            add_regular_expense(name, amount)
            await update.message.reply_text(f"✅ Регулярный расход «{html.escape(name)}» добавлен ({amount:.2f} ₽)", parse_mode="HTML")
        except ValueError:
            await update.message.reply_text("Ошибка при добавлении регулярного расхода")
        return

    # Добавление цели накопления
    if context.user_data.get("adding_goal"):
        context.user_data.pop("adding_goal")
        match = EXPENSE_RE.match(text)
        if not match:
            await update.message.reply_text(
                "Неверный формат. Используй: название сумма\nНапример: Отпуск 150000"
            )
            return
        name = match.group(1).strip()
        try:
            amount = float(match.group(2).replace(",", "."))
            goal_id = add_goal(name, amount)
            if goal_id == -1:
                await update.message.reply_text(f"Цель «{html.escape(name)}» уже существует", parse_mode="HTML")
            else:
                await update.message.reply_text(f"✅ Цель «{html.escape(name)}» создана на {amount:.2f} ₽", parse_mode="HTML")
        except ValueError:
            await update.message.reply_text("Ошибка при добавлении цели")
        return

    # Пополнение цели
    if context.user_data.get("contrib_mode"):
        context.user_data.pop("contrib_mode")
        try:
            amount = float(text.replace(",", "."))
            goal_id = context.user_data.pop("contrib_goal_id")
            goal = next((g for g in get_goals() if g[0] == goal_id), None)
            if goal:
                goal_id, name, target, current = goal
                new_current = current + amount
                if update_goal(goal_id, new_current):
                    percentage = (new_current / target * 100) if target > 0 else 0
                    filled = int(percentage / 10)
                    bar = "█" * filled + "░" * (10 - filled)
                    await update.message.reply_text(
                        f"✅ Пополнено!\n\n🎯 {html.escape(name)}\n{bar} {new_current:.0f}/{target:.0f} ₽ ({percentage:.0f}%)",
                        parse_mode="HTML"
                    )
        except ValueError:
            await update.message.reply_text("Ошибка: введи число")
        return

    # Редактирование названия расхода
    if context.user_data.get("edit_type") == "name":
        expense_id = context.user_data.pop("edit_expense_id")
        context.user_data.pop("edit_type")
        if update_expense(expense_id, item=text.strip()):
            await update.message.reply_text(f"✅ Название изменено на '{html.escape(text.strip())}'", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Ошибка при изменении названия")
        return

    # Редактирование суммы расхода
    if context.user_data.get("edit_type") == "amount":
        expense_id = context.user_data.pop("edit_expense_id")
        context.user_data.pop("edit_type")
        try:
            amount = float(text.replace(",", "."))
            if amount <= 0:
                await update.message.reply_text("Сумма должна быть положительной")
                return
            if update_expense(expense_id, amount=amount):
                await update.message.reply_text(f"✅ Сумма изменена на {amount:.2f} ₽")
            else:
                await update.message.reply_text("❌ Ошибка при изменении суммы")
        except ValueError:
            await update.message.reply_text("Ошибка: введи число")
        return

    # Ввод дат для собственного диапазона графика
    if context.user_data.get("chart_mode") == "waiting_start":
        try:
            start_date = datetime.strptime(text, "%d.%m.%Y").date()
            context.user_data["chart_start"] = start_date
            context.user_data["chart_mode"] = "waiting_end"
            await update.message.reply_text("Введите дату окончания в формате ДД.MM.ГГГГ")
        except ValueError:
            await update.message.reply_text("Неверный формат даты. Используй ДД.MM.ГГГГ")
        return

    if context.user_data.get("chart_mode") == "waiting_end":
        try:
            end_date = datetime.strptime(text, "%d.%m.%Y").date()
            start_date = context.user_data.pop("chart_start")
            context.user_data.pop("chart_mode")

            if start_date > end_date:
                await update.message.reply_text("Дата начала не может быть позже даты окончания")
                return

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_name, item, amount, category, created_at FROM expenses WHERE date(created_at) BETWEEN ? AND ? ORDER BY created_at DESC",
                (start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")),
            )
            timeline = cursor.fetchall()
            conn.close()

            if not timeline:
                await update.message.reply_text(f"📊 Нет данных с {start_date.strftime('%d.%m.%Y')} по {end_date.strftime('%d.%m.%Y')}")
                return

            # Собираем расходы по категориям
            categories_dict = {}
            for user_name, item, amount, category, created_at in timeline:
                if category not in categories_dict:
                    categories_dict[category] = 0
                categories_dict[category] += amount

            # Создаём диаграмму
            fig, ax = plt.subplots(figsize=(10, 8))
            categories = list(categories_dict.keys())
            amounts = list(categories_dict.values())

            ax.pie(amounts, labels=categories, autopct='%1.1f%%', startangle=90)
            ax.set_title(f"Расходы по категориям с {start_date.strftime('%d.%m.%Y')} по {end_date.strftime('%d.%m.%Y')}")

            # Сохраняем в BytesIO
            img_buffer = BytesIO()
            plt.savefig(img_buffer, format='png', bbox_inches='tight', dpi=100)
            img_buffer.seek(0)
            plt.close(fig)

            month_name = f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}"
            await update.message.reply_photo(photo=img_buffer, caption=f"📊 График за период {month_name}")

        except ValueError:
            await update.message.reply_text("Неверный формат даты. Используй ДД.MM.ГГГГ")
        return

    match = EXPENSE_RE.match(text)
    if not match:
        await update.message.reply_text(
            "Не понял. Отправь, пожалуйста, расход в формате: название сумма\n"
            "Например: кофе 300"
        )
        return

    item = match.group(1).strip()
    amount_text = match.group(2).replace(",", ".")
    try:
        amount = float(amount_text)
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом, например: 300 или 250.50")
        return

    context.user_data["pending_expense"] = {"item": item, "amount": amount}
    await update.message.reply_text(
        f"Запись: {item} — {amount:.2f}\nВыберите категорию:",
        reply_markup=build_categories_keyboard(),
    )


async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await query.answer(text="Доступ запрещен", show_alert=True)
        return

    await query.answer()
    
    # Обработка отмены только что добавленного расхода
    if query.data.startswith("cancel_"):
        expense_id = int(query.data.split("_")[1])
        if delete_expense(expense_id):
            await query.edit_message_text("❌ Запись отменена")
        else:
            await query.edit_message_text("⚠️ Не удалось отменить запись")
        return
    
    # Обработка удаления из списка последних расходов
    if query.data.startswith("delete_"):
        expense_id = int(query.data.split("_")[1])
        if delete_expense(expense_id):
            await query.edit_message_text("✅ Запись удалена")
        else:
            await query.edit_message_text("⚠️ Не удалось удалить запись")
        return
    
    # Обработка отчетов
    if query.data.startswith("report_"):
        today = date.today()
        if query.data == "report_current":
            year, month = today.year, today.month
            month_name = today.strftime("%B %Y")
        elif query.data == "report_previous":
            first_of_month = today.replace(day=1)
            last_month = first_of_month - timedelta(days=1)
            year, month = last_month.year, last_month.month
            month_name = last_month.strftime("%B %Y")
        else:
            return
        
        total, by_user, timeline = get_monthly_report(year, month)
        report_text = format_report(total, by_user, timeline, month_name)
        
        # Проверяем длину сообщения
        if len(report_text) > 4000:  # Запас на форматирование
            # Создаем CSV файл
            csv_file = create_csv_report(total, by_user, timeline, month_name)
            csv_content = csv_file.getvalue()
            
            # Отправляем файл
            await query.message.reply_document(
                document=io.BytesIO(csv_content.encode('utf-8-sig')),
                filename=f"report_{year}_{month:02d}.csv",
                caption=f"📊 Отчет за {month_name} (слишком длинный для сообщения)",
            )
            await query.edit_message_text("📄 Отчет отправлен файлом")
        else:
            await query.edit_message_text(report_text, parse_mode="HTML")
        return
    
    # Обработка меню-кнопок
    if query.data == "menu_stats":
        stats = get_monthly_stats()
        month_name = date.today().strftime("%B %Y")
        
        report = f"📊 <b>Отчет за {month_name}</b>\n\n"
        report += f"💰 <b>Общие траты:</b> {stats['total']:.2f} ₽\n\n"
        report += "👥 <b>По членам семьи:</b>\n"
        if stats['by_user']:
            for user_name, amount in stats['by_user']:
                report += f"  • {user_name}: {amount:.2f} ₽\n"
        else:
            report += "  (нет данных)\n"
        report += "\n"
        report += "⏱️ <b>Последние 3 операции:</b>\n"
        if stats['last_three']:
            for user_name, item, amount, category, created_at in stats['last_three']:
                dt = datetime.fromisoformat(created_at)
                time_str = dt.strftime("%d.%m %H:%M")
                report += f"  {time_str} {html.escape(item)} — {amount:.2f} ₽ ({html.escape(category)}) | {html.escape(user_name)}\n"
        else:
            report += "  (нет данных)\n"

        await query.edit_message_text(report, parse_mode="HTML")
        return
    
    if query.data == "menu_help":
        await query.edit_message_text(
            "💡 <b>Как использовать бота:</b>\n\n"
            "1️⃣ Отправь сообщение в формате: <b>название сумма</b>\n"
            "   Например: <i>пицца 450</i>\n\n"
            "2️⃣ Выбери категорию из предложенных кнопок\n\n"
            "3️⃣ Расход будет сохранен в БД\n\n"
            "/stats — просмотреть отчет за месяц\n"
            "/recent — последние расходы с возможностью удаления\n"
            "/report — детальный отчет с выбором периода",
            parse_mode="HTML",
        )
        return
    
    # Добавление новой категории
    if query.data == "new_category":
        context.user_data["adding_category"] = True
        await query.edit_message_text("Введите название новой категории:")
        return

    # Удаление кастомной категории
    if query.data.startswith("delcat_"):
        cat_id = int(query.data.split("_")[1])
        delete_custom_category(cat_id)
        custom = get_custom_categories()
        if custom:
            keyboard = [
                [InlineKeyboardButton(f"❌ {name}", callback_data=f"delcat_{cid}")]
                for cid, name in custom
            ]
            keyboard.append([InlineKeyboardButton("« Назад", callback_data="menu_help")])
            await query.edit_message_text(
                "🗂 <b>Свои категории</b>\nНажми на категорию, чтобы удалить:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            await query.edit_message_text("Своих категорий нет.")
        return

    # Обработка графиков (диаграмм)
    if query.data.startswith("chart_"):
        today = date.today()
        try:
            if query.data == "chart_current":
                year, month = today.year, today.month
                month_name = today.strftime("%B %Y")
            elif query.data == "chart_previous":
                first_of_month = today.replace(day=1)
                last_month = first_of_month - timedelta(days=1)
                year, month = last_month.year, last_month.month
                month_name = last_month.strftime("%B %Y")
            elif query.data == "chart_custom_start":
                await query.edit_message_text("Введите дату начала в формате ДД.MM.ГГГГ")
                context.user_data["chart_mode"] = "waiting_start"
                return
            else:
                return

            total, by_user, timeline = get_monthly_report(year, month)

            # Собираем расходы по категориям
            categories_dict = {}
            for user_name, item, amount, category, created_at in timeline:
                if category not in categories_dict:
                    categories_dict[category] = 0
                categories_dict[category] += amount

            if not categories_dict:
                await query.edit_message_text(f"📊 Нет данных за {month_name}")
                return

            # Создаём диаграмму
            fig, ax = plt.subplots(figsize=(10, 8))
            categories = list(categories_dict.keys())
            amounts = list(categories_dict.values())

            ax.pie(amounts, labels=categories, autopct='%1.1f%%', startangle=90)
            ax.set_title(f"Расходы по категориям за {month_name}")

            # Сохраняем в BytesIO
            img_buffer = BytesIO()
            plt.savefig(img_buffer, format='png', bbox_inches='tight', dpi=100)
            img_buffer.seek(0)
            plt.close(fig)

            await query.message.reply_photo(photo=img_buffer, caption=f"📊 График за {month_name}")
            await query.edit_message_text("📊 График отправлен")

        except ValueError as e:
            await query.answer(text=f"Ошибка при создании графика: {e}", show_alert=True)
        return

    # Обработка регулярных расходов
    if query.data == "new_regular":
        context.user_data["adding_regular"] = True
        await query.edit_message_text("Введите название и сумму регулярного расхода в формате: название сумма\nНапример: интернет 1500")
        return

    if query.data.startswith("add_regular_"):
        exp_id = int(query.data.split("_")[-1])
        regular = get_regular_expenses()
        expense = next((e for e in regular if e[0] == exp_id), None)
        if expense:
            context.user_data["pending_expense"] = {"item": expense[1], "amount": expense[2]}
            await query.edit_message_text(
                f"Запись: {expense[1]} — {expense[2]:.2f}\nВыберите категорию:",
                reply_markup=build_categories_keyboard(),
            )

    if query.data.startswith("del_regular_"):
        exp_id = int(query.data.split("_")[-1])
        if delete_regular_expense(exp_id):
            await query.answer("✅ Регулярный расход удален")
            regular = get_regular_expenses()
            keyboard = []
            for r_id, name, amount in regular:
                keyboard.append([
                    InlineKeyboardButton(f"➕ {name} ({amount:.2f}₽)", callback_data=f"add_regular_{r_id}"),
                    InlineKeyboardButton("❌", callback_data=f"del_regular_{r_id}")
                ])
            keyboard.append([InlineKeyboardButton("➕ Добавить регулярный", callback_data="new_regular")])
            await query.edit_message_text(
                "📋 <b>Регулярные расходы:</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        return

    # Обработка целей
    if query.data == "new_goal":
        context.user_data["adding_goal"] = True
        await query.edit_message_text("Введите название и сумму цели в формате: название сумма\nНапример: Отпуск 150000")
        return

    if query.data.startswith("contrib_goal_"):
        goal_id = int(query.data.split("_")[-1])
        context.user_data["contrib_goal_id"] = goal_id
        context.user_data["contrib_mode"] = True
        await query.edit_message_text("Введите сумму для пополнения")
        return

    if query.data.startswith("del_goal_"):
        goal_id = int(query.data.split("_")[-1])
        if delete_goal(goal_id):
            await query.answer("✅ Цель удалена")
            goals = get_goals()
            keyboard = []
            text = "🎯 <b>Цели накопления:</b>\n\n"

            for g_id, name, target, current in goals:
                percentage = (current / target * 100) if target > 0 else 0
                filled = int(percentage / 10)
                bar = "█" * filled + "░" * (10 - filled)
                text += f"{bar} {current:.0f}/{target:.0f} ₽ ({percentage:.0f}%)\n"
                text += f"  {html.escape(name)}\n\n"
                keyboard.append([
                    InlineKeyboardButton(f"➕ Пополнить", callback_data=f"contrib_goal_{g_id}"),
                    InlineKeyboardButton("❌", callback_data=f"del_goal_{g_id}")
                ])

            keyboard.append([InlineKeyboardButton("➕ Добавить цель", callback_data="new_goal")])
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Обработка редактирования расходов
    if query.data.startswith("edit_expense_"):
        expense_id = int(query.data.split("_")[-1])
        keyboard = [
            [InlineKeyboardButton("Название", callback_data=f"edit_name_{expense_id}")],
            [InlineKeyboardButton("Сумму", callback_data=f"edit_amount_{expense_id}")],
        ]
        await query.edit_message_text(
            "Что вы хотите изменить?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if query.data.startswith("edit_name_") or query.data.startswith("edit_amount_"):
        parts = query.data.split("_")
        expense_id = int(parts[-1])
        edit_type = "name" if "name" in query.data else "amount"
        context.user_data["edit_expense_id"] = expense_id
        context.user_data["edit_type"] = edit_type
        if edit_type == "name":
            await query.edit_message_text("Введите новое название")
        else:
            await query.edit_message_text("Введите новую сумму")
        return

    # Обработка категорий расходов
    if not query.data.startswith("cat_"):
        return

    pending = context.user_data.get("pending_expense")
    if not pending:
        await query.edit_message_text(
            "Сначала отправь расход в формате: название сумма, например: обед 350"
        )
        return

    category = query.data[len("cat_"):]
    expense_id = add_expense(
        user_id=user_id,
        user_name=USER_NAMES.get(user_id, update.effective_user.full_name or "Неизвестный"),
        item=pending["item"],
        amount=pending["amount"],
        category=category,
    )
    context.user_data.pop("pending_expense", None)

    user_name = USER_NAMES.get(user_id, update.effective_user.full_name or "Неизвестный")

    # Проверка месячного бюджета
    limit_str = get_setting("monthly_limit")
    if limit_str:
        limit = float(limit_str)
        stats = get_monthly_stats()
        total = stats['total']
        remaining = limit - total

        if total > limit:
            await query.message.reply_text(f"🚨 <b>Бюджет на месяц превышен!</b>\nРасход: {total:.2f} ₽ из {limit:.2f} ₽", parse_mode="HTML")
        elif remaining < limit * 0.2:
            percentage = (remaining / limit) * 100
            await query.message.reply_text(f"⚠️ <b>Внимание! Осталось {remaining:.2f} ₽ из {limit:.2f} ₽ бюджета ({percentage:.0f}%)</b>", parse_mode="HTML")

    # Отправка уведомления в семейный чат
    if FAMILY_CHAT_ID:
        notification = f"💸 {html.escape(user_name)} добавил: {html.escape(pending['item'])} — {pending['amount']:.2f} ₽ ({html.escape(category)})"
        try:
            await context.bot.send_message(chat_id=FAMILY_CHAT_ID, text=notification, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление в семейный чат: {e}")

    # Кнопка отмены
    keyboard = [[InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{expense_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"✅ Сохранено: {pending['item']} — {pending['amount']:.2f} ₽\nКатегория: {category}\nДобавил: {user_name}",
        reply_markup=reply_markup
    )


async def weekly_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Получаем текущую дату и вычисляем начало и конец недели (пн-вс)
    today = date.today()
    # Сводку отправляем только по воскресеньям (понедельник=0 ... воскресенье=6)
    if today.weekday() != 6:
        return
    # Вычисляем понедельник этой недели
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    start_str = monday.strftime("%Y-%m-%d")
    end_str = sunday.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Общая сумма за неделю
    cursor.execute(
        "SELECT SUM(amount) FROM expenses WHERE date(created_at) BETWEEN ? AND ?",
        (start_str, end_str),
    )
    total = cursor.fetchone()[0] or 0

    # По пользователям
    cursor.execute(
        "SELECT user_id, SUM(amount) FROM expenses WHERE date(created_at) BETWEEN ? AND ? GROUP BY user_id ORDER BY SUM(amount) DESC",
        (start_str, end_str),
    )
    by_user_raw = cursor.fetchall()
    by_user = [(USER_NAMES.get(user_id, f"ID:{user_id}"), amount) for user_id, amount in by_user_raw]

    # По категориям
    cursor.execute(
        "SELECT category, SUM(amount) FROM expenses WHERE date(created_at) BETWEEN ? AND ? GROUP BY category ORDER BY SUM(amount) DESC",
        (start_str, end_str),
    )
    by_category = cursor.fetchall()

    conn.close()

    # Форматируем и отправляем сводку
    week_str = f"{monday.strftime('%d.%m')} - {sunday.strftime('%d.%m.%Y')}"
    text = f"📊 <b>Еженедельная сводка</b>\n<b>{week_str}</b>\n\n"
    text += f"💰 <b>Всего за неделю:</b> {total:.2f} ₽\n\n"

    text += "👥 <b>По членам семьи:</b>\n"
    if by_user:
        for user_name, amount in by_user:
            text += f"  • {html.escape(user_name)}: {amount:.2f} ₽\n"
    else:
        text += "  (нет данных)\n"

    text += "\n🏷️ <b>По категориям:</b>\n"
    if by_category:
        for category, amount in by_category:
            text += f"  • {html.escape(category)}: {amount:.2f} ₽\n"
    else:
        text += "  (нет данных)\n"

    # Отправляем обоим пользователям
    for user_id in ALLOWED_USER_IDS:
        try:
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка при отправке еженедельной сводки пользователю {user_id}: {e}")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is not set")

    init_db()

    app = ApplicationBuilder().token(token).build()

    # Добавляем обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("diag", diag_command))
    app.add_handler(CommandHandler("models", models_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("recent", recent_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("categories", categories_command))
    app.add_handler(CommandHandler("setlimit", setlimit_command))
    app.add_handler(CommandHandler("regular", regular_command))
    app.add_handler(CommandHandler("chart", chart_command))
    app.add_handler(CommandHandler("goals", goals_command))
    app.add_handler(CommandHandler("addgoal", addgoal_command))
    app.add_handler(CommandHandler("contribute", contribute_command))

    # Обработчик фото ПЕРЕД текстовым (важно!)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_category))

    # Настраиваем еженедельную сводку (воскресенье, 19:00 МСК).
    # В python-telegram-bot нет run_weekly, поэтому запускаем ежедневно в 19:00 МСК,
    # а сама функция weekly_summary отправляет сводку только по воскресеньям.
    job_queue = app.job_queue
    job_queue.run_daily(
        weekly_summary,
        time=dt_time(hour=19, minute=0, tzinfo=MOSCOW_TZ),
        name="weekly_summary",
    )

    async def post_init(app):
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Начало"),
                BotCommand("stats", "Статистика за месяц"),
                BotCommand("recent", "Последние расходы"),
                BotCommand("report", "Детальный отчет"),
                BotCommand("categories", "Управление своими категориями"),
                BotCommand("setlimit", "Установить месячный лимит"),
                BotCommand("regular", "Регулярные расходы"),
                BotCommand("chart", "График по категориям"),
                BotCommand("goals", "Цели накопления"),
                BotCommand("addgoal", "Добавить цель"),
                BotCommand("contribute", "Пополнить цель"),
                BotCommand("diag", "Проверка распознавания чеков"),
                BotCommand("models", "Список доступных моделей"),
                BotCommand("help", "Справка"),
            ]
        )

    app.post_init = post_init
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
