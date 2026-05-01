import logging
import os
import re
import sqlite3
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand
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

# Разрешенные пользователи (владелец и жена)
ALLOWED_USER_IDS = [652328822, 970623315]  # 652328822 - ты, 970623315 - Катя

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
    conn.commit()
    conn.close()


def add_expense(user_id: int, user_name: str, item: str, amount: float, category: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO expenses (user_id, user_name, item, amount, category, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, user_name, item, amount, category, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


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
        "SELECT user_name, SUM(amount) FROM expenses WHERE created_at LIKE ? GROUP BY user_name ORDER BY SUM(amount) DESC",
        (f"{current_month}%",),
    )
    by_user = cursor.fetchall()
    
    # Топ-3 категории
    cursor.execute(
        "SELECT category, SUM(amount) FROM expenses WHERE created_at LIKE ? GROUP BY category ORDER BY SUM(amount) DESC LIMIT 3",
        (f"{current_month}%",),
    )
    top_categories = cursor.fetchall()
    
    conn.close()
    
    return {
        "total": total,
        "by_user": by_user,
        "top_categories": top_categories,
    }


def build_categories_keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=cat, callback_data=cat) for cat in CATEGORIES]
    keyboard = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
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


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return
    
    stats = get_monthly_stats()
    
    # Оформляем отчет
    from datetime import date
    month_name = date.today().strftime("%B %Y")
    
    report = f"📊 <b>Отчет за {month_name}</b>\n\n"
    
    # Общая сумма
    report += f"💰 <b>Общие траты:</b> {stats['total']:.2f} ₽\n\n"
    
    # По пользователям
    report += "👥 <b>По членам семьи:</b>\n"
    if stats['by_user']:
        for user_name, amount in stats['by_user']:
            report += f"  • {user_name}: {amount:.2f} ₽\n"
    else:
        report += "  (нет данных)\n"
    
    report += "\n"
    
    # Топ-3 категории
    report += "🏆 <b>Топ-3 категории:</b>\n"
    if stats['top_categories']:
        for i, (category, amount) in enumerate(stats['top_categories'], 1):
            report += f"  {i}. {category}: {amount:.2f} ₽\n"
    else:
        report += "  (нет данных)\n"
    
    await update.message.reply_text(report, parse_mode="HTML")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Доступ запрещен")
        return

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
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await query.answer(text="Доступ запрещен", show_alert=True)
        return

    await query.answer()
    
    # Обработка меню-кнопок
    if query.data == "menu_stats":
        stats = get_monthly_stats()
        from datetime import date
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
        report += "🏆 <b>Топ-3 категории:</b>\n"
        if stats['top_categories']:
            for i, (category, amount) in enumerate(stats['top_categories'], 1):
                report += f"  {i}. {category}: {amount:.2f} ₽\n"
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
            "/stats — просмотреть отчет за месяц",
            parse_mode="HTML",
        )
        return
    
    # Обработка категорий расходов
    pending = context.user_data.get("pending_expense")
    if not pending:
        await query.edit_message_text(
            "Сначала отправь расход в формате: название сумма, например: обед 350"
        )
        return

    category = query.data
    add_expense(
        user_id=user_id,
        user_name=update.effective_user.full_name,
        item=pending["item"],
        amount=pending["amount"],
        category=category,
    )
    context.user_data.pop("pending_expense", None)

    await query.edit_message_text(
        f"Сохранено: {pending['item']} — {pending['amount']:.2f} ₽\nКатегория: {category}\nДобавил: {update.effective_user.full_name}"
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
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_category))

    async def post_init(app):
        """Установить меню команд в Telegram"""
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Начало"),
                BotCommand("stats", "Статистика за месяц"),
                BotCommand("help", "Справка"),
            ]
        )

    app.post_init = post_init
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
