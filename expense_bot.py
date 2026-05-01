import logging
import os
import re
import sqlite3
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

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

EXPENSE_RE = re.compile(r"^(.+?)\s+(\d+(?:[.,]\d+)?)$")


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            item TEXT,
            amount REAL,
            category TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def add_expense(user_id: int, item: str, amount: float, category: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO expenses (user_id, item, amount, category, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, item, amount, category, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def build_categories_keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=cat, callback_data=cat) for cat in CATEGORIES]
    keyboard = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Отправь расход в формате: название сумма\n"
        "Например: кофе 300\n"
        "После этого выбери категорию из кнопок, и я сохраню запись."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Используй сообщение в формате 'пицца 450' или 'такси 120'.\n"
        "Затем выбери категорию."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
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
    await query.answer()

    pending = context.user_data.get("pending_expense")
    if not pending:
        await query.edit_message_text(
            "Сначала отправь расход в формате: название сумма, например: обед 350"
        )
        return

    category = query.data
    add_expense(
        user_id=update.effective_user.id,
        item=pending["item"],
        amount=pending["amount"],
        category=category,
    )
    context.user_data.pop("pending_expense", None)

    await query.edit_message_text(
        f"Сохранено: {pending['item']} — {pending['amount']:.2f} ₽\nКатегория: {category}"
    )


def main() -> None:
    token = os.environ.get(
        "TELEGRAM_BOT_TOKEN",
        "8679634637:AAECrey0UQN9kgdmpYrnHeyqrfwidI6_RwI",
    )

    init_db()

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_category))

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
