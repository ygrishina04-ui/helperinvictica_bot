import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TIMEZONE = os.getenv("REPORTS_TIMEZONE", "Asia/Vladivostok")
DB_NAME = os.getenv("REPORTS_DB_NAME", "daily_summaries.db")

# Настройки трех ежедневных напоминаний по статусам заказов.
# STATUS_CHAT_ID — чат, куда бот отправляет задания и повторные напоминания.
# FINAL_STATUS_CHAT_ID — другой чат, куда отправляется только итоговое сообщение.
STATUS_CHAT_ID = os.getenv("STATUS_CHAT_ID", "").strip()
FINAL_STATUS_CHAT_ID = os.getenv("FINAL_STATUS_CHAT_ID", "").strip()
STATUS_REACTION_EMOJI = "✅"

DMITRIY_USER_ID = 337526112
EVGENIY_USER_ID = 7839493170

STATUS_WEEKDAYS = {0, 1, 2, 3, 4}

STATUS_REMINDERS = [
    {
        "task_type": "booking",
        "send_time": "10:00",
        "deadline_time": "11:00",
        "text": (
            "⏰ Напоминание\n\n"
            "С 10:00 до 11:00 необходимо обновить все заказы "
            "в статусе «Букинг»\n"
            "@dk_shekhovtcov\n\n"
            "После выполнения поставьте реакцию ✅ на это сообщение."
        ),
        "responsible": [
            (DMITRIY_USER_ID, "dk_shekhovtcov"),
        ],
    },
    {
        "task_type": "sea",
        "send_time": "11:00",
        "deadline_time": "11:30",
        "text": (
            "⏰ Напоминание\n\n"
            "С 11:00 до 11:30 необходимо обновить все заказы "
            "в статусе «Море»\n"
            "@dk_shekhovtcov, @Osipov_INV\n\n"
            "После выполнения поставьте реакцию ✅ на это сообщение."
        ),
        "responsible": [
            (DMITRIY_USER_ID, "dk_shekhovtcov"),
            (EVGENIY_USER_ID, "Osipov_INV"),
        ],
    },
    {
        "task_type": "port",
        "send_time": "11:30",
        "deadline_time": "12:00",
        "text": (
            "⏰ Напоминание\n\n"
            "С 11:30 до 12:00 необходимо обновить все заказы "
            "в статусе «Порт», а также обновить ДО1, "
            "дополнительные меры и отгрузку по ЖД\n"
            "@Osipov_INV\n\n"
            "После выполнения поставьте реакцию ✅ на это сообщение."
        ),
        "responsible": [
            (EVGENIY_USER_ID, "Osipov_INV"),
        ],
    },
]

_DB_LOCK = threading.Lock()


def get_db_connection():
    conn = sqlite3.connect(
        DB_NAME,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_reports_db():
    with _DB_LOCK:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                summary_date TEXT,
                created INTEGER,
                calculated INTEGER,
                not_created INTEGER,
                hanging INTEGER,
                without_feedback INTEGER,
                passed_rate INTEGER,
                passed_clients TEXT,
                raw_text TEXT,
                created_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS reaction_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                responsible_user_id INTEGER NOT NULL,
                responsible_username TEXT,
                required_emoji TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT,
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                reminder_sent_at TEXT,
                last_reaction_at TEXT,
                UNIQUE(chat_id, message_id, responsible_user_id)
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_reaction_tasks_pending
            ON reaction_tasks(completed, reminder_sent, deadline_at)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS status_final_messages (
                task_date TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )
        """)

        conn.commit()
        conn.close()


def parse_summary(text):
    data = {
        "summary_date": None,
        "created": 0,
        "calculated": 0,
        "not_created": 0,
        "hanging": 0,
        "without_feedback": 0,
        "passed_rate": 0,
        "passed_clients": [],
    }

    date_match = re.search(r"Сводка за:\s*(\d{2}/\d{2})", text, re.IGNORECASE)
    if date_match:
        data["summary_date"] = date_match.group(1)

    patterns = {
        "created": r"Запросов заведено\s*(\d+)",
        "calculated": r"Запросов посчитано\s*(\d+)",
        "not_created": r"Запросов не заведено\s*(\d+)",
        "hanging": r"Зависшие запросы\s*(\d+)",
        "without_feedback": r"Запросов без ОС\s*(\d+)",
        "passed_rate": r"Прошли по ставке:?\s*(\d+)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            data[key] = int(match.group(1))

    lines = text.splitlines()
    collect_clients = False

    for line in lines:
        line = line.strip()

        if not line:
            continue

        if line.lower().startswith("прошли по ставке"):
            collect_clients = True
            continue

        if collect_clients:
            data["passed_clients"].append(line)

    return data


def save_daily_summary(message):
    text = message.get("text", "")
    parsed = parse_summary(text)

    chat_id = message["chat"]["id"]

    user = message.get("from", {})
    user_id = user.get("id")
    username = user.get("username", "")
    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip()

    now = datetime.now(ZoneInfo(TIMEZONE))
    passed_clients_text = "\n".join(parsed["passed_clients"])

    with _DB_LOCK:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO daily_summaries (
                chat_id,
                user_id,
                username,
                full_name,
                summary_date,
                created,
                calculated,
                not_created,
                hanging,
                without_feedback,
                passed_rate,
                passed_clients,
                raw_text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chat_id,
            user_id,
            username,
            full_name,
            parsed["summary_date"],
            parsed["created"],
            parsed["calculated"],
            parsed["not_created"],
            parsed["hanging"],
            parsed["without_feedback"],
            parsed["passed_rate"],
            passed_clients_text,
            text,
            now.strftime("%Y-%m-%d %H:%M:%S"),
        ))

        conn.commit()
        conn.close()

    print("Сводка сохранена!", flush=True)
    print(parsed, flush=True)


def handle_report_message(message, send_message_func):
    text = message.get("text", "").strip()
    chat_id = message["chat"]["id"]

    if not text:
        return
        
    if text == "/test_status":
        send_message_func(
        int(STATUS_CHAT_ID),
        "🧪 Тестовое сообщение"
    )

        send_message_func(
        chat_id,
        "✅ Команда обработана"
    )
    return
    
    if text.lower().startswith("сводка за:"):
        save_daily_summary(message)
        send_message_func(chat_id, "✅ Сводка принята")
        return

    if text == "/weekly_report":
        report = build_weekly_report(chat_id)
        send_long_message(send_message_func, chat_id, report)
        return



def build_weekly_report(chat_id):
    today = datetime.now(ZoneInfo(TIMEZONE)).date()

    last_monday = today - timedelta(days=8)
    last_sunday = today

    start_dt = datetime.combine(
        last_monday,
        datetime.min.time(),
    ).strftime("%Y-%m-%d %H:%M:%S")

    end_dt = datetime.combine(
        last_sunday,
        datetime.max.time(),
    ).strftime("%Y-%m-%d %H:%M:%S")

    with _DB_LOCK:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                summary_date,
                full_name,
                created,
                calculated,
                not_created,
                hanging,
                without_feedback,
                passed_rate,
                passed_clients
            FROM daily_summaries
            WHERE chat_id = ?
            AND created_at BETWEEN ? AND ?
            ORDER BY created_at ASC
        """, (chat_id, start_dt, end_dt))

        rows = cur.fetchall()
        conn.close()

    if not rows:
        return "За прошлую неделю сводок не найдено."

    total_created = 0
    total_calculated = 0
    total_not_created = 0
    total_hanging = 0
    total_without_feedback = 0
    total_passed_rate = 0
    all_clients = []
    daily_blocks = []

    for row in rows:
        summary_date = row["summary_date"]
        full_name = row["full_name"] or "Без имени"
        created = row["created"]
        calculated = row["calculated"]
        not_created = row["not_created"]
        hanging = row["hanging"]
        without_feedback = row["without_feedback"]
        passed_rate = row["passed_rate"]
        passed_clients = row["passed_clients"]

        total_created += created
        total_calculated += calculated
        total_not_created += not_created
        total_hanging += hanging
        total_without_feedback += without_feedback
        total_passed_rate += passed_rate

        if passed_clients:
            all_clients.extend(passed_clients.splitlines())

        daily_blocks.append(
            f"📅 {summary_date} — {full_name}\n"
            f"Заведено: {created}, посчитано: {calculated}, "
            f"не заведено: {not_created}, зависшие: {hanging}, "
            f"без ОС: {without_feedback}, прошли по ставке: {passed_rate}"
        )

    clients_text = (
        "\n".join(f"— {client}" for client in all_clients)
        if all_clients
        else "— нет"
    )

    return (
        "📊 Итоговая сводка за неделю\n"
        f"{last_monday.strftime('%d.%m')}–{last_sunday.strftime('%d.%m')}\n\n"
        "ИТОГО:\n"
        f"Запросов заведено: {total_created}\n"
        f"Запросов посчитано: {total_calculated}\n"
        f"Запросов не заведено: {total_not_created}\n"
        f"Зависшие запросы: {total_hanging}\n"
        f"Запросов без ОС: {total_without_feedback}\n"
        f"Прошли по ставке: {total_passed_rate}\n\n"
        "Клиенты, которые прошли по ставке:\n"
        f"{clients_text}\n\n"
        "Детализация по дням:\n"
        f"{chr(10).join(daily_blocks)}"
    )


def send_long_message(send_message_func, chat_id, text):
    max_len = 3900

    for i in range(0, len(text), max_len):
        send_message_func(chat_id, text[i:i + max_len])


def parse_clock(value):
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Некорректное время: {value}")
    return hour, minute


def create_status_task(send_message_func, reminder):
    now = datetime.now(ZoneInfo(TIMEZONE))
    deadline_hour, deadline_minute = parse_clock(reminder["deadline_time"])
    deadline = now.replace(
        hour=deadline_hour,
        minute=deadline_minute,
        second=0,
        microsecond=0,
    )

    sent_message = send_message_func(int(STATUS_CHAT_ID), reminder["text"])
    message_id = sent_message["message_id"]

    with _DB_LOCK:
        conn = get_db_connection()
        cur = conn.cursor()
        for user_id, username in reminder["responsible"]:
            cur.execute("""
                INSERT OR REPLACE INTO reaction_tasks (
                    task_type, chat_id, message_id,
                    responsible_user_id, responsible_username,
                    required_emoji, sent_at, deadline_at,
                    completed, completed_at,
                    reminder_sent, reminder_sent_at, last_reaction_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 0, NULL, NULL)
            """, (
                reminder["task_type"],
                int(STATUS_CHAT_ID),
                int(message_id),
                int(user_id),
                username,
                STATUS_REACTION_EMOJI,
                now.strftime("%Y-%m-%d %H:%M:%S"),
                deadline.strftime("%Y-%m-%d %H:%M:%S"),
            ))
        conn.commit()
        conn.close()

    print(
        f"Отправлено задание {reminder['task_type']}: message_id={message_id}",
        flush=True,
    )


def handle_report_reaction(
    chat_id,
    message_id,
    user_id,
    reaction_emojis,
    send_message_func=None,
):
    now = datetime.now(ZoneInfo(TIMEZONE))
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")

    with _DB_LOCK:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, required_emoji
            FROM reaction_tasks
            WHERE chat_id = ?
              AND message_id = ?
              AND responsible_user_id = ?
            LIMIT 1
        """, (int(chat_id), int(message_id), int(user_id)))
        task = cur.fetchone()

        if not task:
            conn.close()
            return

        completed = 1 if task["required_emoji"] in reaction_emojis else 0
        cur.execute("""
            UPDATE reaction_tasks
            SET completed = ?, completed_at = ?, last_reaction_at = ?
            WHERE id = ?
        """, (
            completed,
            now_text if completed else None,
            now_text,
            task["id"],
        ))
        conn.commit()
        conn.close()

    if completed and send_message_func:
        check_and_send_final_status(send_message_func, now.date())


def build_reminder_text(task):
    username = (task["responsible_username"] or "").strip()
    mention = f"@{username}, " if username else ""
    return (
        f"⏰ {mention}задача ещё не подтверждена.\n\n"
        f"После выполнения поставьте {task['required_emoji']} "
        "на исходное сообщение."
    )


def process_due_reaction_tasks(send_message_func):
    now = datetime.now(ZoneInfo(TIMEZONE))
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")

    with _DB_LOCK:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT *
            FROM reaction_tasks
            WHERE completed = 0
              AND reminder_sent = 0
              AND deadline_at <= ?
            ORDER BY deadline_at ASC
        """, (now_text,))
        tasks = cur.fetchall()
        conn.close()

    for task in tasks:
        try:
            send_message_func(
                task["chat_id"],
                build_reminder_text(task),
                reply_to_message_id=task["message_id"],
            )
            with _DB_LOCK:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    UPDATE reaction_tasks
                    SET reminder_sent = 1, reminder_sent_at = ?
                    WHERE id = ? AND completed = 0 AND reminder_sent = 0
                """, (now_text, task["id"]))
                conn.commit()
                conn.close()
        except Exception as error:
            print(f"ERROR SENDING REMINDER task_id={task['id']}: {error}", flush=True)


def check_and_send_final_status(send_message_func, task_date):
    if not FINAL_STATUS_CHAT_ID:
        return

    date_text = task_date.strftime("%Y-%m-%d")
    day_start = f"{date_text} 00:00:00"
    day_end = f"{date_text} 23:59:59"

    with _DB_LOCK:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute(
            "SELECT 1 FROM status_final_messages WHERE task_date = ?",
            (date_text,),
        )
        if cur.fetchone():
            conn.close()
            return

        cur.execute("""
            SELECT task_type,
                   COUNT(*) AS total_count,
                   SUM(completed) AS completed_count
            FROM reaction_tasks
            WHERE sent_at BETWEEN ? AND ?
              AND task_type IN ('booking', 'sea', 'port')
            GROUP BY task_type
        """, (day_start, day_end))
        rows = cur.fetchall()
        status = {
            row["task_type"]: row["total_count"] == row["completed_count"]
            for row in rows
        }

        all_done = (
            status.get("booking") is True
            and status.get("sea") is True
            and status.get("port") is True
        )

        if not all_done:
            conn.close()
            return

        cur.execute("""
            INSERT INTO status_final_messages (task_date, sent_at)
            VALUES (?, ?)
        """, (date_text, datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()

    try:
        send_message_func(
            int(FINAL_STATUS_CHAT_ID),
            "✅ Заказы в статусе: букинг, море, порт — полностью обновлены.",
        )
    except Exception:
        # Разрешаем повторную попытку, если Telegram не принял сообщение.
        with _DB_LOCK:
            conn = get_db_connection()
            conn.execute(
                "DELETE FROM status_final_messages WHERE task_date = ?",
                (date_text,),
            )
            conn.commit()
            conn.close()
        raise


def weekly_report_loop(send_message_func):
    last_sent_date = None

    while True:
        now = datetime.now(ZoneInfo(TIMEZONE))

        if now.weekday() == 0 and now.hour == 9 and now.minute == 0:
            today = now.strftime("%Y-%m-%d")

            if last_sent_date != today:
                with _DB_LOCK:
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute("SELECT DISTINCT chat_id FROM daily_summaries")
                    chats = cur.fetchall()
                    conn.close()

                for chat in chats:
                    chat_id = chat["chat_id"]
                    report = build_weekly_report(chat_id)
                    send_long_message(send_message_func, chat_id, report)

                last_sent_date = today

        time.sleep(30)


def reaction_reminder_loop(send_message_func):
    while True:
        try:
            process_due_reaction_tasks(send_message_func)
            check_and_send_final_status(
                send_message_func,
                datetime.now(ZoneInfo(TIMEZONE)).date(),
            )
        except Exception as error:
            print(f"ERROR IN REACTION LOOP: {error}", flush=True)
        time.sleep(30)


def status_schedule_loop(send_message_func):
    if not STATUS_CHAT_ID:
        print("Статусные напоминания отключены: не задан STATUS_CHAT_ID", flush=True)
        return

    try:
        int(STATUS_CHAT_ID)
        if FINAL_STATUS_CHAT_ID:
            int(FINAL_STATUS_CHAT_ID)
        for reminder in STATUS_REMINDERS:
            parse_clock(reminder["send_time"])
            parse_clock(reminder["deadline_time"])
    except ValueError as error:
        print(f"Статусные напоминания отключены: {error}", flush=True)
        return

    sent_today = set()
    current_date = None

    while True:
        now = datetime.now(ZoneInfo(TIMEZONE))
        today = now.strftime("%Y-%m-%d")

        if current_date != today:
            current_date = today
            sent_today.clear()

        if now.weekday() in STATUS_WEEKDAYS:
            for reminder in STATUS_REMINDERS:
                send_hour, send_minute = parse_clock(reminder["send_time"])
                task_type = reminder["task_type"]
                if (
                    now.hour == send_hour
                    and now.minute == send_minute
                    and task_type not in sent_today
                ):
                    try:
                        create_status_task(send_message_func, reminder)
                        sent_today.add(task_type)
                    except Exception as error:
                        print(
                            f"ERROR SENDING STATUS TASK {task_type}: {error}",
                            flush=True,
                        )
        time.sleep(20)


def start_weekly_reports(send_message_func):
    weekly_thread = threading.Thread(
        target=weekly_report_loop,
        args=(send_message_func,),
        daemon=True,
        name="weekly-reports",
    )
    weekly_thread.start()

    reminder_thread = threading.Thread(
        target=reaction_reminder_loop,
        args=(send_message_func,),
        daemon=True,
        name="reaction-reminders",
    )
    reminder_thread.start()

    status_thread = threading.Thread(
        target=status_schedule_loop,
        args=(send_message_func,),
        daemon=True,
        name="status-schedule",
    )
    status_thread.start()

    print(
        "Модуль отчетов, реакций и статусных напоминаний запущен",
        flush=True,
    )
