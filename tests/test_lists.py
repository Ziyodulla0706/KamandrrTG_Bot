from datetime import date, datetime, time, timezone

from taskbot.dates import to_utc
from taskbot.db import Task
from taskbot.formatting import render_list, sort_tasks
from taskbot.keyboards import list_keyboard
from taskbot.texts import SCOPE_ALL, SCOPE_TODAY
from tests.helpers import TZ


def test_sort_puts_timed_tasks_before_timeless_inside_a_day():
    day = date(2026, 11, 16)
    tasks = [
        _task(1, day, None),
        _task(2, day, time(15, 0)),
        _task(3, day, time(9, 0)),
        _task(4, day.fromordinal(day.toordinal() + 1), time(8, 0)),
    ]
    assert [task.id for task in sort_tasks(tasks)] == [3, 2, 1, 4]


def test_today_separates_overdue_and_does_not_mark_timeless_until_midnight():
    today = date(2026, 11, 16)
    now = datetime(2026, 11, 16, 12, 0, tzinfo=TZ).astimezone(timezone.utc)
    tasks = [
        _task(1, date(2026, 11, 15), None),
        _task(2, today, time(9, 0)),
        _task(3, today, None),
        _task(4, today, time(18, 0)),
        _task(5, date(2026, 11, 17), time(8, 0)),
    ]
    text, visible, page, pages = render_list(
        SCOPE_TODAY,
        tasks,
        tz=TZ,
        today=today,
        now_utc=now,
        page=0,
    )
    assert page == 0 and pages == 1
    assert "Просроченные" in text
    assert "На сегодня" in text
    assert [task.id for task in visible] == [1, 2, 4, 3]
    overdue_part, today_part = text.split("<b>На сегодня</b>")
    assert "15.11.2026" in overdue_part
    assert "09:00" in overdue_part
    assert "без времени" in today_part
    assert "18:00" in today_part
    assert "17.11.2026" not in text


def test_pagination_respects_message_length_and_callback_limit():
    today = date(2026, 11, 16)
    tasks = [
        _task(index, date(2026, 11, 16).fromordinal(date(2026, 11, 16).toordinal() + index), None, title="Задача " + ("длинное название " * 8) + str(index))
        for index in range(1, 13)
    ]
    text, visible, page, pages = render_list(
        SCOPE_ALL,
        tasks,
        tz=TZ,
        today=today,
        now_utc=datetime(2026, 11, 16, 12, tzinfo=TZ).astimezone(timezone.utc),
        page=0,
    )
    assert pages == 3
    assert page == 0
    assert len(visible) == 5
    assert len(text) < 4096
    assert "Страница 1 из 3" in text
    markup = list_keyboard(visible, SCOPE_ALL, 0, pages, TZ)
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(button.callback_data == "l:al:1" for button in buttons)
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
    assert all(len(button.text) <= 64 for button in buttons)
    assert all(not button.text.startswith("ID") for button in buttons)


def _task(task_id: int, task_date: date, clock: time | None, title: str = "Задача") -> Task:
    due = to_utc(task_date, clock, TZ) if clock is not None else None
    stamp = datetime(2026, 11, 1, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        owner_id=42,
        title=title,
        source_text=title,
        task_date=task_date,
        due_at_utc=due,
        status="open",
        created_at=stamp,
        updated_at=stamp,
        completed_at=None,
        source_chat_id=1,
        source_message_id=task_id,
        source_line=1,
    )
