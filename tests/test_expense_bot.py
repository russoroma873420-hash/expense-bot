"""Офлайн-тесты семейного бота расходов.

Работают на временной БД, без сети и без обращения к Telegram API.
Запуск из корня репозитория:  python -m pytest tests -q
"""

from __future__ import annotations

import ast
import asyncio
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import expense_bot as bot  # noqa: E402

SOURCE = (ROOT / "expense_bot.py").read_text(encoding="utf-8")

ALLOWED_ID = bot.ALLOWED_USER_IDS[0]
STRANGER_ID = 111111111


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Временная БД на каждый тест: модуль читает bot.DB_PATH в момент вызова."""
    monkeypatch.setattr(bot, "DB_PATH", str(tmp_path / "expenses.db"))
    bot.init_db()
    return bot.DB_PATH


# --- схема и базовые операции с БД -------------------------------------------------


def _tables(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {name for (name,) in rows}


def test_init_db_creates_all_tables(db):
    assert {"expenses", "custom_categories", "settings", "regular_expenses", "goals"} <= _tables(db)


def test_add_and_read_back_expense(db):
    expense_id = bot.add_expense(ALLOWED_ID, "Александр", "кофе", 300.0, "Кафе")
    rows = bot.get_recent_expenses(10)
    assert len(rows) == 1
    row_id, user_name, item, amount, category, created_at = rows[0]
    assert (row_id, user_name, item, amount, category) == (expense_id, "Александр", "кофе", 300.0, "Кафе")
    assert datetime.fromisoformat(created_at)  # дата разбирается обратно


def test_delete_expense_reports_whether_row_existed(db):
    expense_id = bot.add_expense(ALLOWED_ID, "Александр", "такси", 120.0, "Транспорт")
    assert bot.delete_expense(expense_id) is True
    assert bot.delete_expense(expense_id) is False


def test_update_expense_only_touches_whitelisted_fields(db):
    expense_id = bot.add_expense(ALLOWED_ID, "Александр", "обед", 350.0, "Кафе")
    assert bot.update_expense(expense_id, amount=400.0) is True
    assert bot.update_expense(expense_id, category="Другое") is False  # поле не разрешено
    assert bot.update_expense(expense_id) is False  # нечего менять
    _id, _name, _item, amount, category, _created = bot.get_recent_expenses(1)[0]
    assert (amount, category) == (400.0, "Кафе")


def test_custom_category_is_unique_and_joins_builtin_list(db):
    assert bot.add_custom_category("Ипотека") is True
    assert bot.add_custom_category("Ипотека") is False
    assert "Ипотека" in bot.get_all_categories()
    assert bot.get_all_categories()[: len(bot.CATEGORIES)] == bot.CATEGORIES

    cat_id = bot.get_custom_categories()[0][0]
    assert bot.delete_custom_category(cat_id) is True
    assert bot.delete_custom_category(cat_id) is False


def test_settings_roundtrip(db):
    assert bot.get_setting("monthly_limit") is None
    bot.set_setting("monthly_limit", "50000")
    assert bot.get_setting("monthly_limit") == "50000"
    bot.set_setting("monthly_limit", "60000")
    assert bot.get_setting("monthly_limit") == "60000"


def test_goals_lifecycle(db):
    goal_id = bot.add_goal("Отпуск", 150000.0)
    assert bot.add_goal("Отпуск", 1.0) == -1  # имя уникально
    assert bot.get_goal_by_name("Отпуск")[0] == goal_id
    assert bot.update_goal(goal_id, 5000.0) is True
    assert bot.get_goals() == [(goal_id, "Отпуск", 150000.0, 5000.0)]
    assert bot.delete_goal(goal_id) is True
    assert bot.delete_goal(goal_id) is False


def test_regular_expenses_crud(db):
    exp_id = bot.add_regular_expense("интернет", 1500.0)
    assert bot.get_regular_expenses() == [(exp_id, "интернет", 1500.0)]
    assert bot.delete_regular_expense(exp_id) is True
    assert bot.get_regular_expenses() == []


# --- разбор ввода и отчёты ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "item", "amount"),
    [
        ("кофе 300", "кофе", 300.0),
        ("такси 120,50", "такси", 120.5),
        ("пицца маргарита 650.5", "пицца маргарита", 650.5),
    ],
)
def test_expense_regex_parses_input(text, item, amount):
    match = bot.EXPENSE_RE.match(text)
    assert match is not None
    assert match.group(1).strip() == item
    assert float(match.group(2).replace(",", ".")) == amount


@pytest.mark.parametrize("text", ["кофе", "300", "", "кофе рублей"])
def test_expense_regex_rejects_garbage(text):
    assert bot.EXPENSE_RE.match(text) is None


def test_format_report_escapes_user_input():
    report = bot.format_report(
        300.0,
        [("<b>Александр</b>", 300.0)],
        [("Иван", "<script>x</script>", 300.0, "Кафе &lt;b&gt;", "2025-01-01T10:00:00")],
        "январь",
    )
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert "&lt;b&gt;Александр&lt;/b&gt;" in report


def test_create_csv_report_contains_totals_and_headers():
    csv_text = bot.create_csv_report(
        300.0,
        [("Александр", 300.0)],
        [("Александр", "кофе", 300.0, "Кафе", "2025-01-01T10:00:00")],
        "январь 2025",
    ).getvalue()
    assert "Общие траты;300.00 руб." in csv_text
    assert "Дата;Время;Наименование;Сумма;Категория;Кто добавил" in csv_text
    assert "01.01.2025;10:00;кофе;300.00;Кафе;Александр" in csv_text


def test_monthly_report_totals_by_user(db):
    bot.add_expense(ALLOWED_ID, "Александр", "кофе", 300.0, "Кафе")
    bot.add_expense(bot.ALLOWED_USER_IDS[1], "Екатерина", "такси", 200.0, "Транспорт")
    # берём период из фактически записанной даты: add_expense пишет UTC
    created = bot.get_recent_expenses(1)[0][5]
    stamp = datetime.fromisoformat(created)

    total, by_user, timeline = bot.get_monthly_report(stamp.year, stamp.month)
    assert total == 500.0
    assert dict(by_user) == {"Александр": 300.0, "Екатерина": 200.0}
    assert len(timeline) == 2


def test_monthly_stats_uses_current_month_marker(db, monkeypatch):
    """Статистика берёт текущий месяц по Барнаулу, не подмешивая прошлый."""
    _freeze_today(monkeypatch, date(2026, 9, 22))
    current = "2026-09-15T10:00:00"
    previous = "2026-08-31T10:00:00"

    conn = sqlite3.connect(db)
    try:
        for created_at, item, amount in ((current, "кофе", 300.0), (previous, "старое", 999.0)):
            conn.execute(
                "INSERT INTO expenses (user_id, user_name, item, amount, category, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ALLOWED_ID, "Александр", item, amount, "Кафе", created_at),
            )
        conn.commit()
    finally:
        conn.close()

    stats = bot.get_monthly_stats()
    assert stats["total"] == 300.0  # прошлый месяц не подмешивается
    assert stats["last_three"][0][1] == "кофе"


def test_expense_timestamp_is_local_barnaul(db):
    """Время пишется по Барнаулу и без смещения.

    Без смещения — потому что SQLite date() пересчитывает значение со смещением
    в UTC, и тогда расход, внесённый вечером, попал бы в следующий день.
    """
    before = bot.local_now().replace(tzinfo=None)
    bot.add_expense(ALLOWED_ID, "Александр", "кофе", 300.0, "Кафе")
    after = bot.local_now().replace(tzinfo=None)

    created = datetime.fromisoformat(bot.get_recent_expenses(1)[0][5])
    assert created.tzinfo is None
    assert before <= created <= after

    # местное время опережает UTC ровно на смещение Барнаула
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert timedelta(hours=6, minutes=59) < (created - utc_now) < timedelta(hours=7, minutes=1)


# --- обработчики (с подставными Update/Context) ------------------------------------


class _Message:
    def __init__(self, text: str = ""):
        self.text = text
        self.sent: list[str] = []

    async def reply_text(self, text, **_kwargs):
        self.sent.append(text)


class _User:
    def __init__(self, user_id: int):
        self.id = user_id
        self.full_name = "Тестовый"


class _Update:
    def __init__(self, user_id: int, text: str = ""):
        self.effective_user = _User(user_id)
        self.message = _Message(text)


class _Context:
    def __init__(self):
        self.user_data: dict = {}


def test_handle_message_asks_for_category(db):
    update, context = _Update(ALLOWED_ID, "кофе 300"), _Context()
    asyncio.run(bot.handle_message(update, context))

    assert context.user_data["pending_expense"] == {"item": "кофе", "amount": 300.0}
    assert "Выберите категорию" in update.message.sent[-1]


def test_handle_message_denies_stranger(db):
    update, context = _Update(STRANGER_ID, "кофе 300"), _Context()
    asyncio.run(bot.handle_message(update, context))

    assert "pending_expense" not in context.user_data
    assert update.message.sent == ["Доступ запрещен"]


def test_handle_message_reports_bad_format(db):
    update, context = _Update(ALLOWED_ID, "просто текст"), _Context()
    asyncio.run(bot.handle_message(update, context))

    assert "pending_expense" not in context.user_data
    assert "название сумма" in update.message.sent[-1]


class _CallbackQuery:
    def __init__(self, data: str):
        self.data = data
        self.message = _Message()

    async def answer(self, **_kwargs):
        return None

    async def edit_message_text(self, text, **_kwargs):
        self.message.sent.append(text)


class _CallbackUpdate(_Update):
    def __init__(self, user_id: int, data: str):
        super().__init__(user_id)
        self.callback_query = _CallbackQuery(data)


def test_choosing_category_saves_expense(db):
    update = _CallbackUpdate(ALLOWED_ID, f"cat_{bot.CATEGORIES[0]}")
    context = _Context()
    context.user_data["pending_expense"] = {"item": "кофе", "amount": 300.0}

    asyncio.run(bot.handle_category(update, context))

    assert "pending_expense" not in context.user_data
    _id, _name, item, amount, category, _created = bot.get_recent_expenses(1)[0]
    assert (item, amount, category) == ("кофе", 300.0, bot.CATEGORIES[0])


def test_callback_denies_stranger(db):
    update, context = _CallbackUpdate(STRANGER_ID, f"cat_{bot.CATEGORIES[0]}"), _Context()
    asyncio.run(bot.handle_category(update, context))

    assert bot.get_recent_expenses(10) == []


# --- инварианты, которые ломались в реальных багах ---------------------------------


def _emitted_callbacks() -> set[str]:
    """Все callback_data, которые бот реально рассылает в кнопках."""
    emitted: set[str] = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "callback_data":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                emitted.add(value.value)
            elif isinstance(value, ast.JoinedStr) and value.values:
                head = value.values[0]
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    emitted.add(head.value)  # префикс вида "cat_{cat}"
    return emitted


def _handled_callbacks() -> tuple[set[str], set[str]]:
    """Точные значения и префиксы, которые разбирает handle_category."""
    handler = SOURCE.split("async def handle_category", 1)[1].split("\nasync def ", 1)[0]
    exact = set(re.findall(r'query\.data == "([^"]+)"', handler))
    prefixes = set(re.findall(r'query\.data\.startswith\("([^"]+)"\)', handler))
    prefixes |= set(re.findall(r'query\.data\[len\("([^"]+)"\):\]', handler))
    return exact, prefixes


def test_every_button_callback_is_handled():
    """Регресс на баг /chart: кнопка отправляла callback, которого не ждал обработчик."""
    exact, prefixes = _handled_callbacks()
    unhandled = sorted(
        cb for cb in _emitted_callbacks() if cb not in exact and not cb.startswith(tuple(prefixes))
    )
    assert unhandled == [], f"кнопки с необработанным callback_data: {unhandled}"
    assert "chart_custom_start" in _emitted_callbacks()


def test_no_hardcoded_telegram_token():
    """Токен бота не должен попадать в исходник: он читается из окружения."""
    leaks = re.findall(r"\d{8,10}:[A-Za-z0-9_-]{30,}", SOURCE)
    assert leaks == [], "в expense_bot.py закоммичен токен бота"


def test_main_requires_token_from_environment(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        bot.main()


def test_schedule_runs_daily_at_19_00_barnaul():
    """19:00 по Барнаулу (UTC+7); день недели проверяет сама weekly_summary."""
    from telegram.ext import ApplicationBuilder

    application = ApplicationBuilder().token("123456:AAaaBBbbCCccDDdd").build()
    job = application.job_queue.run_daily(
        bot.weekly_summary,
        time=time(19, 0, tzinfo=bot.LOCAL_TZ),
        name="weekly_summary",
    )
    next_fire = job.trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    local = next_fire.astimezone(bot.LOCAL_TZ)

    assert (local.hour, local.minute) == (19, 0)
    assert local.utcoffset() == timedelta(hours=7)
    today_local = datetime.now(timezone.utc).astimezone(bot.LOCAL_TZ).date()
    assert (local.date() - today_local).days <= 1  # задача ежедневная, не раз в неделю


def test_days_parameter_uses_cron_numbering():
    """Ловушка, из-за которой сводка не уходила: days в PTB ≠ date.weekday().

    days=(6,) планирует субботу, а не воскресенье, и функция, которая проверяет
    date.weekday() == 6, при таком расписании не срабатывает никогда.
    """
    from telegram.ext import ApplicationBuilder

    application = ApplicationBuilder().token("123456:AAaaBBbbCCccDDdd").build()
    job = application.job_queue.run_daily(
        bot.weekly_summary,
        time=time(19, 0, tzinfo=bot.LOCAL_TZ),
        days=(6,),
        name="cron-numbering",
    )
    next_fire = job.trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    assert next_fire.astimezone(bot.LOCAL_TZ).weekday() == 5  # суббота, а не воскресенье


class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **_kwargs):
        self.sent.append((chat_id, text))


class _JobContext:
    def __init__(self):
        self.bot = _FakeBot()


def _freeze_today(monkeypatch, day: date):
    """Подменяет «сегодня по Барнаулу», не трогая реальные часы машины."""
    monkeypatch.setattr(bot, "local_today", lambda: day)


def test_weekly_summary_skips_non_sunday(db, monkeypatch):
    _freeze_today(monkeypatch, date(2026, 9, 23))  # среда
    context = _JobContext()
    asyncio.run(bot.weekly_summary(context))
    assert context.bot.sent == []


def test_weekly_summary_sends_to_both_users_on_sunday(db, monkeypatch):
    _freeze_today(monkeypatch, date(2026, 9, 27))  # воскресенье
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO expenses (user_id, user_name, item, amount, category, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ALLOWED_ID, "Александр", "кофе", 300.0, "Кафе", "2026-09-23T10:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    context = _JobContext()
    asyncio.run(bot.weekly_summary(context))

    assert [chat_id for chat_id, _text in context.bot.sent] == list(bot.ALLOWED_USER_IDS)
    assert "300.00" in context.bot.sent[0][1]


def test_job_queue_available_with_extra():
    """Без extra job-queue app.job_queue is None и бот падал на старте."""
    from telegram.ext import ApplicationBuilder

    application = ApplicationBuilder().token("123456:AAaaBBbbCCccDDdd").build()
    assert application.job_queue is not None
    job = application.job_queue.run_daily(
        bot.weekly_summary,
        time=time(19, 0, tzinfo=bot.LOCAL_TZ),
        days=(6,),
        name="weekly_summary",
    )
    assert job.name == "weekly_summary"


def test_rejected_duplicates_do_not_lock_the_database(db):
    """Регресс: отклонённый дубликат оставлял открытую транзакцию и блокировал запись."""
    assert bot.add_custom_category("Ипотека") is True
    assert bot.add_custom_category("Ипотека") is False  # путь IntegrityError
    assert bot.add_goal("Отпуск", 150000.0) > 0
    assert bot.add_goal("Отпуск", 1.0) == -1  # путь IntegrityError
    assert bot.set_setting("monthly_limit", "50000") is None

    # раньше именно здесь прилетало sqlite3.OperationalError: database is locked
    expense_id = bot.add_expense(ALLOWED_ID, "Александр", "кофе", 300.0, "Кафе")
    assert expense_id > 0
    assert len(bot.get_recent_expenses(10)) == 1
    assert bot.delete_expense(expense_id) is True
