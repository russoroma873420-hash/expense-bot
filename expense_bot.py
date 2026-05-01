import logging
import os
import re
import sqlite3
import csv
import io
from datetime import datetime, date, timedelta

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
    conn.commit()
    conn.close()


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
            report += f"  • {user_name}: {amount:.2f} ₽\n"
    else:
        report += "  (нет данных)\n"
    
    report += "\n📅 <b>Хронология:</b>\n"
    if timeline:
        for user_name, item, amount, category, created_at in timeline:
            dt = datetime.fromisoformat(created_at)
            time_str = dt.strftime("%d.%m %H:%M")
            report += f"  {time_str} {item} — {amount:.2f} ₽ ({category}) | {user_name}\n"
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
        text += f"• {time_str} {item} — {amount:.2f} ₽ ({category}) | {user_name}\n"
        keyboard.append([InlineKeyboardButton(f"❌ Удалить: {item[:20]}...", callback_data=f"delete_{expense_id}")])
    
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
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "📊 Выберите период для отчета:",
        reply_markup=reply_markup
    )


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
            "/stats — просмотреть отчет за месяц\n"
            "/recent — последние расходы с возможностью удаления\n"
            "/report — детальный отчет с выбором периода",
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
    expense_id = add_expense(
        user_id=user_id,
        user_name=USER_NAMES.get(user_id, update.effective_user.full_name or "Неизвестный"),
        item=pending["item"],
        amount=pending["amount"],
        category=category,
    )
    context.user_data.pop("pending_expense", None)

    # Кнопка отмены
    keyboard = [[InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{expense_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ Сохранено: {pending['item']} — {pending['amount']:.2f} ₽\nКатегория: {category}\nДобавил: {USER_NAMES.get(user_id, update.effective_user.full_name or 'Неизвестный')}",
        reply_markup=reply_markup
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
    app.add_handler(CommandHandler("recent", recent_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_category))

    async def post_init(app):
        """Установить меню команд в Telegram"""
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Начало"),
                BotCommand("stats", "Статистика за месяц"),
                BotCommand("recent", "Последние расходы"),
                BotCommand("report", "Детальный отчет"),
                BotCommand("help", "Справка"),
            ]
        )

    app.post_init = post_init
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
