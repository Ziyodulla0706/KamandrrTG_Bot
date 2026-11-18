from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from taskbot.dates import is_overdue
from taskbot.db import Task
from taskbot.formatting import MESSAGE_LIMIT, h, local_clock, sort_tasks

WEEK_DAYS = 7
WEEK_PAGE_SIZE = 8
TITLE_LIMIT = 70
WEEKDAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def week_tasks(tasks: list[Task], today: date) -> list[Task]:
    """Открытые задачи на семь дней, включая сегодня. Дни до сегодняшнего не входят."""

    last = today + timedelta(days=WEEK_DAYS - 1)
    chosen = [
        task
        for task in tasks
        if task.status == "open" and today <= task.task_date <= last
    ]
    return sort_tasks(chosen)


def count_earlier_overdue(tasks: list[Task], today: date, now_utc: datetime) -> int:
    return sum(
        1
        for task in tasks
        if task.status == "open"
        and task.task_date < today
        and is_overdue(
            task.task_date, task.due_at_utc, task.status, today=today, now_utc=now_utc
        )
    )


def day_heading(day: date, today: date) -> str:
    name = WEEKDAY_NAMES[day.weekday()]
    suffix = " (сегодня)" if day == today else ""
    if day == today + timedelta(days=1):
        suffix = " (завтра)"
    return f"<b>{name} {day.strftime('%d.%m')}{suffix}</b>"


def render_week(
    tasks: list[Task],
    *,
    tz: ZoneInfo,
    today: date,
    now_utc: datetime,
    page: int,
    categories: dict[int, str],
    earlier_overdue: int = 0,
) -> tuple[str, list[Task], int, int]:
    items = week_tasks(tasks, today)
    pages = max(1, (len(items) + WEEK_PAGE_SIZE - 1) // WEEK_PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    chunk = items[page * WEEK_PAGE_SIZE : (page + 1) * WEEK_PAGE_SIZE]
    last = today + timedelta(days=WEEK_DAYS - 1)
    lines = [f"📅 Неделя, {today.strftime('%d.%m')} – {last.strftime('%d.%m.%Y')}"]
    if not items:
        lines.append("")
        lines.append("На эти дни открытых задач нет.")
    current: date | None = None
    for task in chunk:
        if task.task_date != current:
            current = task.task_date
            lines.append("")
            lines.append(day_heading(current, today))
        lines.append(_line(task, tz, today, now_utc, categories))
    if earlier_overdue and page == 0:
        lines.append("")
        lines.append(f"Просроченных с прошлых дней: {earlier_overdue}. Они в разделе «Сегодня».")
    if pages > 1:
        lines.append("")
        lines.append(f"Страница {page + 1} из {pages}")
    text = "\n".join(lines)
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 1] + "…"
    return text, chunk, page, pages


def _line(
    task: Task,
    tz: ZoneInfo,
    today: date,
    now_utc: datetime,
    categories: dict[int, str],
) -> str:
    clock = local_clock(task, tz)
    when = clock.strftime("%H:%M") if clock else "без времени"
    title = task.title if len(task.title) <= TITLE_LIMIT else task.title[: TITLE_LIMIT - 1] + "…"
    parts = [f"• {when} — {h(title)}"]
    if task.recurrence_id is not None:
        parts.append("🔁")
    category = categories.get(task.category_id or -1)
    if category:
        parts.append(f"🏷 {h(category)}")
    if is_overdue(task.task_date, task.due_at_utc, task.status, today=today, now_utc=now_utc):
        parts.append("(просрочена)")
    return " ".join(parts)
