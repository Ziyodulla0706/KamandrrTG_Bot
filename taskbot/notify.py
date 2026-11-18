from datetime import date, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from zoneinfo import ZoneInfo

from taskbot.dates import from_utc, is_overdue
from taskbot.db import Settings, Task
from taskbot.formatting import MESSAGE_LIMIT, format_when, h, sort_tasks

DIGEST_PREVIEW = 5


def digest_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Сегодня", callback_data="l:td:0")],
            [InlineKeyboardButton(text="📋 Все задачи", callback_data="l:al:0")],
        ]
    )


def reminder_keyboard(task_id: int, delivery_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово", callback_data=f"d:{task_id}:al:0")],
            [
                InlineKeyboardButton(
                    text="⏰ Через 15 минут",
                    callback_data=f"sn:{delivery_id}:15",
                ),
                InlineKeyboardButton(
                    text="⏰ Через час",
                    callback_data=f"sn:{delivery_id}:60",
                ),
            ],
            [InlineKeyboardButton(text="📅 Перенести", callback_data=f"m:{task_id}:al:0")],
        ]
    )


def settings_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    digest = "Выключить сводку" if settings.digest_enabled else "Включить сводку"
    reminders = "Выключить напоминания" if settings.reminders_enabled else "Включить напоминания"

    def lead(value: int, label: str) -> InlineKeyboardButton:
        prefix = "• " if settings.reminder_lead_minutes == value else ""
        return InlineKeyboardButton(text=prefix + label, callback_data=f"st:lead:{value}")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=digest, callback_data="st:digest")],
            [InlineKeyboardButton(text="Время сводки", callback_data="st:time")],
            [InlineKeyboardButton(text=reminders, callback_data="st:remind")],
            [lead(0, "В момент задачи"), lead(15, "За 15 минут")],
            [lead(30, "За 30 минут"), lead(60, "За 60 минут")],
        ]
    )


def digest_time_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="st:time:cancel")]]
    )


def render_settings(settings: Settings, timezone_key: str) -> str:
    digest = "включена" if settings.digest_enabled else "выключена"
    reminders = "включены" if settings.reminders_enabled else "выключены"
    if settings.reminder_lead_minutes == 0:
        lead = "в момент задачи"
    else:
        lead = f"за {settings.reminder_lead_minutes} минут"
    return (
        "Настройки\n"
        f"Часовой пояс: {h(timezone_key)}\n"
        f"Утренняя сводка: {digest}, время {settings.digest_time.strftime('%H:%M')}\n"
        f"Напоминания: {reminders}, {lead}\n\n"
        "Часовой пояс берётся из конфигурации и здесь не меняется."
    )


def render_reminder(task: Task, tz: ZoneInfo, *, late: bool, snoozed: bool) -> str:
    lines = ["Отложенное напоминание" if snoozed else "Напоминание"]
    if late and snoozed:
        lines.append("Срок задачи уже прошёл. Это отложенное напоминание.")
    elif late:
        lines.append("Время задачи уже прошло.")
    lines.append(f"<b>{h(task.title)}</b>")
    lines.append(format_when(task, tz))
    return "\n".join(lines)


def render_snooze_confirmation(
    task: Task,
    tz: ZoneInfo,
    when: datetime,
    *,
    already: bool,
) -> str:
    local = from_utc(when, tz).strftime("%d.%m.%Y %H:%M")
    due = format_when(task, tz, compact=True)
    if already:
        head = f"Это напоминание уже назначено на {local}."
    else:
        head = f"Напоминание отложено до {local}."
    return f"{head}\nСрок задачи не изменился: {due}."


def render_digest(
    tasks: list[Task],
    tz: ZoneInfo,
    today: date,
    now_utc: datetime,
) -> str | None:
    overdue, timed, timeless = _split_digest(tasks, tz, today, now_utc)
    if not overdue and not timed and not timeless:
        return None
    header = _digest_header(today, timed, timeless, overdue)
    full = _digest_body(header, timed, timeless, overdue, tz, preview=None)
    if len(full) <= MESSAGE_LIMIT:
        return full
    compact = _digest_body(header, timed, timeless, overdue, tz, preview=DIGEST_PREVIEW)
    if len(compact) <= MESSAGE_LIMIT:
        return compact
    short = header + "\n\nСписок длинный. Откройте его кнопкой."
    if len(short) <= MESSAGE_LIMIT:
        return short
    return short[: MESSAGE_LIMIT - 1] + "…"


def _split_digest(
    tasks: list[Task],
    tz: ZoneInfo,
    today: date,
    now_utc: datetime,
) -> tuple[list[Task], list[Task], list[Task]]:
    del tz
    overdue: list[Task] = []
    timed: list[Task] = []
    timeless: list[Task] = []
    for task in tasks:
        if task.status != "open":
            continue
        if is_overdue(
            task.task_date,
            task.due_at_utc,
            task.status,
            today=today,
            now_utc=now_utc,
        ):
            overdue.append(task)
        elif task.task_date == today:
            if task.due_at_utc is None:
                timeless.append(task)
            else:
                timed.append(task)
    return sort_tasks(overdue), sort_tasks(timed), sort_tasks(timeless)


def _digest_header(
    today: date,
    timed: list[Task],
    timeless: list[Task],
    overdue: list[Task],
) -> str:
    return "\n".join(
        [
            f"Утренняя сводка на {today.strftime('%d.%m.%Y')}",
            f"Открытых на сегодня: {len(timed) + len(timeless)}",
            f"Со временем: {len(timed)}",
            f"Без времени: {len(timeless)}",
            f"Просроченных: {len(overdue)}",
        ]
    )


def _digest_body(
    header: str,
    timed: list[Task],
    timeless: list[Task],
    overdue: list[Task],
    tz: ZoneInfo,
    *,
    preview: int | None,
) -> str:
    lines = [header]
    hidden = 0
    hidden += _append_section(lines, "Задачи со временем", timed, tz, preview)
    hidden += _append_section(lines, "Задачи без времени", timeless, tz, preview)
    hidden += _append_section(lines, "Просроченные", overdue, tz, preview)
    if hidden:
        lines.append("")
        lines.append("Показаны первые задачи. Остальные открываются кнопкой.")
    return "\n".join(lines)


def _append_section(
    lines: list[str],
    title: str,
    tasks: list[Task],
    tz: ZoneInfo,
    preview: int | None,
) -> int:
    if not tasks:
        return 0
    shown = tasks if preview is None else tasks[:preview]
    lines.append("")
    lines.append(title)
    for task in shown:
        lines.append(f"• {h(task.title)} — {format_when(task, tz, compact=True)}")
    hidden = len(tasks) - len(shown)
    if hidden:
        lines.append(f"и ещё {hidden}")
    return hidden
