from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from taskbot.dates import Clock, is_overdue, to_utc
from taskbot.formatting import format_when
from taskbot.db import Task


TZ = ZoneInfo("Asia/Tashkent")


def test_clock_uses_tashkent_when_utc_is_still_previous_day():
    moment = datetime(2026, 3, 21, 0, 30, tzinfo=TZ)
    assert moment.astimezone(timezone.utc).date() == date(2026, 3, 20)
    clock = Clock(TZ, now=moment)
    assert clock.today() == date(2026, 3, 21)
    assert clock.now_utc().date() == date(2026, 3, 20)


def test_task_without_time_is_not_overdue_until_the_local_day_ends():
    task_date = date(2026, 3, 21)
    evening = datetime(2026, 3, 21, 23, 59, tzinfo=TZ)
    assert not is_overdue(
        task_date,
        None,
        "open",
        today=evening.date(),
        now_utc=evening.astimezone(timezone.utc),
    )
    midnight = datetime(2026, 3, 22, 0, 0, tzinfo=TZ)
    assert is_overdue(
        task_date,
        None,
        "open",
        today=midnight.date(),
        now_utc=midnight.astimezone(timezone.utc),
    )


def test_timed_task_becomes_overdue_after_its_minute_not_at_midnight():
    due = to_utc(date(2026, 3, 21), time(10, 0), TZ)
    same_day = date(2026, 3, 21)
    before = datetime(2026, 3, 21, 9, 59, tzinfo=TZ).astimezone(timezone.utc)
    exact = datetime(2026, 3, 21, 10, 0, tzinfo=TZ).astimezone(timezone.utc)
    after = datetime(2026, 3, 21, 10, 0, 1, tzinfo=TZ).astimezone(timezone.utc)
    assert not is_overdue(same_day, due, "open", today=same_day, now_utc=before)
    assert not is_overdue(same_day, due, "open", today=same_day, now_utc=exact)
    assert is_overdue(same_day, due, "open", today=same_day, now_utc=after)
    assert not is_overdue(same_day, due, "done", today=same_day, now_utc=after)


def test_displayed_date_stays_on_the_local_calendar_day():
    due = to_utc(date(2026, 3, 21), time(0, 30), TZ)
    assert due.astimezone(timezone.utc).date() == date(2026, 3, 20)
    task = _task(date(2026, 3, 21), due)
    text = format_when(task, TZ)
    assert "21.03.2026, 00:30" in text
    assert "20.03.2026" not in text


def _task(task_date: date, due) -> Task:
    stamp = datetime(2026, 3, 20, tzinfo=timezone.utc)
    return Task(
        id=1,
        owner_id=1,
        title="Позвонить",
        source_text="исходный",
        task_date=task_date,
        due_at_utc=due,
        status="open",
        created_at=stamp,
        updated_at=stamp,
        completed_at=None,
        source_chat_id=1,
        source_message_id=1,
        source_line=1,
    )
