from datetime import date, datetime, time
from html import escape
from zoneinfo import ZoneInfo

from taskbot.dates import FAR_FUTURE, from_utc, is_overdue
from taskbot.db import Task
from taskbot.texts import SCOPE_DONE, SCOPE_TODAY, SCOPE_TOMORROW

PAGE_SIZE = 5
MESSAGE_LIMIT = 3900


def h(value: str) -> str:
    return escape(value, quote=False)


def local_clock(task: Task, tz: ZoneInfo) -> time | None:
    if task.due_at_utc is None:
        return None
    return from_utc(task.due_at_utc, tz).time().replace(tzinfo=None)


def format_when(task: Task, tz: ZoneInfo, *, compact: bool = False) -> str:
    date_text = task.task_date.strftime("%d.%m.%Y")
    clock = local_clock(task, tz)
    if clock is None:
        if compact:
            return f"{date_text}, без времени"
        return f"{date_text}\nБез времени"
    return f"{date_text}, {clock.strftime('%H:%M')}"


def confirmation(heading: str, task: Task, tz: ZoneInfo) -> str:
    return f"{heading}\n<b>{h(task.title)}</b>\n{format_when(task, tz)}"


def sort_tasks(tasks: list[Task]) -> list[Task]:
    return sorted(
        tasks,
        key=lambda task: (
            task.task_date,
            0 if task.due_at_utc is not None else 1,
            task.due_at_utc or FAR_FUTURE,
            task.id,
        ),
    )


def render_card(
    task: Task,
    tz: ZoneInfo,
    *,
    today: date,
    now_utc: datetime,
    notice: str | None = None,
    category: str | None = None,
) -> str:
    status = "выполнена" if task.status == "done" else "открыта"
    lines: list[str] = []
    if notice:
        lines.append(notice)
    lines.append(f"<b>{h(task.title)}</b>")
    lines.append(format_when(task, tz))
    lines.append(f"Статус: {status}")
    if category:
        lines.append(f"Категория: {h(category)}")
    if task.recurrence_id is not None:
        lines.append(
            "🔁 Повторяющаяся задача (перенесена отдельно)"
            if task.detached
            else "🔁 Повторяющаяся задача"
        )
    if task.note_id is not None:
        lines.append("📝 Создана из заметки")
    if task.status == "open" and is_overdue(
        task.task_date,
        task.due_at_utc,
        task.status,
        today=today,
        now_utc=now_utc,
    ):
        lines.append("Просрочена")
    return "\n".join(lines)


def question_text(item: dict, index: int, total: int) -> str:
    lines = [
        f"Уточнение {index + 1} из {total}",
        f"Строка {item['line']}: «{h(item['source_text'])}»",
    ]
    if item["kind"] == "ambiguous":
        lines.append("Такую дату я не разбираю. Выберите точный день — исходный текст сохраню.")
    elif item["kind"] == "past":
        proposed = date.fromisoformat(item["proposed_date"])
        lines.append(f"Дата {proposed.strftime('%d.%m.%Y')} уже прошла.")
        if item.get("year_inferred"):
            lines.append(
                f"Год не был указан, поэтому беру {proposed.year} "
                "и не переношу дату на следующий год."
            )
        lines.append("Исправьте дату или сохраните задачу как просроченную.")
    else:
        lines.append("Дата не указана. Когда выполнить эту задачу?")
    if item.get("time"):
        lines.append(f"Время {item['time']} сохраню для выбранного дня.")
    return "\n".join(lines)


def batch_summary(
    *,
    saved: list[Task],
    already: list[Task],
    rejected: list[dict],
    tz: ZoneInfo,
    skipped: list[dict] | None = None,
    heading: str | None = None,
) -> str:
    lines: list[str] = []
    if heading:
        lines.append(heading)
    lines.append(f"Сохранено задач: {len(saved)}")
    if already:
        lines.append(f"Уже были сохранены: {len(already)}")
    if saved or already:
        lines.append("")
        for task in saved:
            lines.append(f"• {format_when(task, tz, compact=True)} — {h(task.title)}")
        for task in already:
            lines.append(
                f"• уже была сохранена: {format_when(task, tz, compact=True)} — {h(task.title)}"
            )
    if rejected:
        lines.append("")
        lines.append("Не удалось разобрать:")
        for item in rejected:
            lines.append(f"• строка {item['line']}: {h(item['message'])}")
            lines.append(f"  «{h(item['source_text'])}»")
    if skipped:
        lines.append("")
        lines.append("Не сохранены:")
        for item in skipped:
            lines.append(f"• строка {item['line']}: «{h(item['source_text'])}»")
    return "\n".join(lines).strip()


def render_list(
    scope: str,
    tasks: list[Task],
    *,
    tz: ZoneInfo,
    today: date,
    now_utc: datetime,
    page: int,
) -> tuple[str, list[Task], int, int]:
    """Возвращает текст, задачи страницы, номер страницы и число страниц."""

    entries = _entries(scope, tasks, today, now_utc)
    pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    chunk = entries[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    text = _list_text(scope, entries, chunk, tz, today, now_utc, page, pages)
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 1] + "…"
    visible = [task for _, task in chunk]
    return text, visible, page, pages


def _entries(
    scope: str,
    tasks: list[Task],
    today: date,
    now_utc: datetime,
) -> list[tuple[str, Task]]:
    if scope == SCOPE_DONE:
        return [("done", task) for task in sort_tasks(tasks)]
    open_tasks = sort_tasks([task for task in tasks if task.status == "open"])
    if scope == SCOPE_TODAY:
        overdue = [
            task
            for task in open_tasks
            if is_overdue(
                task.task_date,
                task.due_at_utc,
                task.status,
                today=today,
                now_utc=now_utc,
            )
        ]
        overdue_ids = {task.id for task in overdue}
        current = [
            task
            for task in open_tasks
            if task.task_date == today and task.id not in overdue_ids
        ]
        return [("overdue", task) for task in overdue] + [("today", task) for task in current]
    if scope == SCOPE_TOMORROW:
        tomorrow = today.fromordinal(today.toordinal() + 1)
        return [("item", task) for task in open_tasks if task.task_date == tomorrow]
    return [("item", task) for task in open_tasks]


def _list_text(
    scope: str,
    entries: list[tuple[str, Task]],
    chunk: list[tuple[str, Task]],
    tz: ZoneInfo,
    today: date,
    now_utc: datetime,
    page: int,
    pages: int,
) -> str:
    if scope == SCOPE_TODAY:
        lines = [f"📅 Сегодня, {today.strftime('%d.%m.%Y')}"]
        overdue_all = [task for kind, task in entries if kind == "overdue"]
        today_all = [task for kind, task in entries if kind == "today"]
        shown_overdue = [task for kind, task in chunk if kind == "overdue"]
        shown_today = [task for kind, task in chunk if kind == "today"]
        if page == 0 and not overdue_all:
            lines.append("")
            lines.append("Просроченных нет.")
        elif shown_overdue:
            lines.append("")
            lines.append("<b>Просроченные</b>")
            lines.extend(
                _item_line(task, tz, today, now_utc, mark=False) for task in shown_overdue
            )
        if page == 0 and not today_all:
            lines.append("")
            lines.append("На сегодня открытых задач нет.")
        elif shown_today:
            lines.append("")
            lines.append("<b>На сегодня</b>")
            lines.extend(
                _item_line(task, tz, today, now_utc, mark=False) for task in shown_today
            )
    elif scope == SCOPE_TOMORROW:
        tomorrow = today.fromordinal(today.toordinal() + 1)
        lines = [f"🌅 Завтра, {tomorrow.strftime('%d.%m.%Y')}"]
        if not entries and page == 0:
            lines.append("")
            lines.append("На завтра задач нет.")
        elif chunk:
            lines.append("")
            lines.extend(
                _item_line(task, tz, today, now_utc, mark=False) for _, task in chunk
            )
    elif scope == SCOPE_DONE:
        lines = ["✅ Выполненные"]
        if not entries and page == 0:
            lines.append("")
            lines.append("Выполненных задач нет.")
        elif chunk:
            lines.append("")
            lines.extend(
                _item_line(task, tz, today, now_utc, mark=False) for _, task in chunk
            )
    else:
        lines = ["📋 Все открытые задачи"]
        if not entries and page == 0:
            lines.append("")
            lines.append("Открытых задач нет.")
        elif chunk:
            lines.append("")
            lines.extend(
                _item_line(task, tz, today, now_utc, mark=True) for _, task in chunk
            )
    if pages > 1:
        lines.append("")
        lines.append(f"Страница {page + 1} из {pages}")
    return "\n".join(lines)


def _item_line(
    task: Task,
    tz: ZoneInfo,
    today: date,
    now_utc: datetime,
    *,
    mark: bool,
) -> str:
    when = format_when(task, tz, compact=True)
    suffix = ""
    if mark and is_overdue(
        task.task_date,
        task.due_at_utc,
        task.status,
        today=today,
        now_utc=now_utc,
    ):
        suffix = " (просрочена)"
    return f"• {when} — {h(task.title)}{suffix}"
