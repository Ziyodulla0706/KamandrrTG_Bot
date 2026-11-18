"""Повторяющиеся задачи.

Шаблон хранится отдельно от задач. Конкретные задачи создаются на ближайшие
30 календарных дней, включая сегодня. Все вычисления идут по дате и времени
в часовом поясе пользователя.
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from taskbot.dates import Clock, dump_dt, load_dt, to_utc
from taskbot.db import Database

HORIZON_DAYS = 30
SYNTHETIC_CHAT = -2  # служебный «чат» для идемпотентного ключа экземпляра
KINDS = ("daily", "weekdays", "weekly", "monthly")
WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
WEEKDAY_LONG = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
MAX_TITLE = 200
_UNSET = object()


@dataclass
class Recurrence:
    id: int
    owner_id: int
    title: str
    kind: str
    weekdays: tuple[int, ...]
    month_day: int | None
    start_date: date
    time_local: time | None
    category_id: int | None
    active: bool
    created_at: datetime
    updated_at: datetime


def last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def matches(rec: Recurrence, day: date) -> bool:
    if day < rec.start_date:
        return False
    if rec.kind == "daily":
        return True
    if rec.kind == "weekdays":
        return day.weekday() < 5
    if rec.kind == "weekly":
        return day.weekday() in rec.weekdays
    if rec.kind == "monthly" and rec.month_day is not None:
        target = min(rec.month_day, last_day_of_month(day.year, day.month))
        return day.day == target
    return False


def occurrence_dates(rec: Recurrence, start: date, end: date) -> list[date]:
    begin = max(start, rec.start_date)
    result: list[date] = []
    day = begin
    while day <= end:
        if matches(rec, day):
            result.append(day)
        day += timedelta(days=1)
    return result


def first_date(rec: Recurrence, today: date) -> date | None:
    found = occurrence_dates(rec, today, today + timedelta(days=800))
    return found[0] if found else None


def month_rule_note(month_day: int | None) -> str | None:
    if month_day is not None and month_day >= 29:
        return (
            f"Если в каком-то месяце нет {month_day} числа, "
            "задача будет в последний день этого месяца."
        )
    return None


def describe_schedule(rec: Recurrence) -> str:
    if rec.kind == "daily":
        base = "Каждый день"
    elif rec.kind == "weekdays":
        base = "По будням (понедельник–пятница)"
    elif rec.kind == "weekly":
        names = ", ".join(WEEKDAY_SHORT[index] for index in sorted(rec.weekdays))
        base = f"Каждую неделю: {names}"
    else:
        base = f"Каждый месяц, {rec.month_day} числа"
    clock = rec.time_local.strftime("%H:%M") if rec.time_local else None
    return f"{base}, в {clock}" if clock else f"{base}, без времени"


def validate(
    *,
    title: str,
    kind: str,
    weekdays: tuple[int, ...],
    month_day: int | None,
) -> str:
    cleaned = " ".join((title or "").split())
    if not cleaned:
        raise ValueError("Название не может быть пустым.")
    if len(cleaned) > MAX_TITLE:
        raise ValueError(f"Название длиннее {MAX_TITLE} символов.")
    if kind not in KINDS:
        raise ValueError("Неизвестный вид повторения.")
    if kind == "weekly" and not weekdays:
        raise ValueError("Выберите хотя бы один день недели.")
    if kind == "weekly" and any(day not in range(7) for day in weekdays):
        raise ValueError("Неверный день недели.")
    if kind == "monthly" and (month_day is None or not 1 <= month_day <= 31):
        raise ValueError("Число месяца должно быть от 1 до 31.")
    return cleaned


async def create_recurrence(
    db: Database,
    owner_id: int,
    *,
    title: str,
    kind: str,
    weekdays: tuple[int, ...] = (),
    month_day: int | None = None,
    start_date: date,
    time_local: time | None,
    category_id: int | None,
    now: datetime,
) -> Recurrence:
    cleaned = validate(title=title, kind=kind, weekdays=weekdays, month_day=month_day)
    stamp = dump_dt(now)
    cursor = await db.conn.execute(
        """
        INSERT INTO recurrences (
            owner_id, title, kind, weekdays, month_day, start_date, time_local,
            category_id, active, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            owner_id,
            cleaned,
            kind,
            ",".join(str(day) for day in sorted(set(weekdays))) if kind == "weekly" else "",
            month_day if kind == "monthly" else None,
            start_date.isoformat(),
            time_local.strftime("%H:%M") if time_local else None,
            category_id,
            stamp,
            stamp,
        ),
    )
    await db.conn.commit()
    rec = await get_recurrence(db, owner_id, int(cursor.lastrowid))
    if rec is None:
        raise RuntimeError("Повторение сохранилось, но не читается из базы.")
    return rec


async def get_recurrence(db: Database, owner_id: int, rec_id: int) -> Recurrence | None:
    cursor = await db.conn.execute(
        "SELECT * FROM recurrences WHERE id = ? AND owner_id = ?",
        (rec_id, owner_id),
    )
    row = await cursor.fetchone()
    return _rec(row) if row is not None else None


async def list_recurrences(db: Database, owner_id: int) -> list[Recurrence]:
    cursor = await db.conn.execute(
        "SELECT * FROM recurrences WHERE owner_id = ? ORDER BY active DESC, id ASC",
        (owner_id,),
    )
    return [_rec(row) for row in await cursor.fetchall()]


async def generate_horizon(db: Database, clock: Clock, owner_id: int) -> int:
    """Достраивает экземпляры от сегодняшнего дня на 30 дней. Повтор безопасен."""

    today = clock.today()
    end = today + timedelta(days=HORIZON_DAYS - 1)
    created_total = 0
    for rec in await list_recurrences(db, owner_id):
        if rec.active:
            created_total += await _generate_one(db, clock, rec, today, end)
    return created_total


async def generate_for(db: Database, clock: Clock, owner_id: int, rec_id: int) -> int:
    rec = await get_recurrence(db, owner_id, rec_id)
    if rec is None or not rec.active:
        return 0
    today = clock.today()
    return await _generate_one(db, clock, rec, today, today + timedelta(days=HORIZON_DAYS - 1))


async def _generate_one(
    db: Database,
    clock: Clock,
    rec: Recurrence,
    today: date,
    end: date,
) -> int:
    wanted = occurrence_dates(rec, today, end)
    if not wanted:
        return 0
    cursor = await db.conn.execute(
        "SELECT occurrence_date FROM tasks WHERE recurrence_id = ? AND occurrence_date >= ?",
        (rec.id, wanted[0].isoformat()),
    )
    known = {str(row["occurrence_date"]) for row in await cursor.fetchall()}
    cursor = await db.conn.execute(
        "SELECT occurrence_date FROM recurrence_exceptions WHERE recurrence_id = ?",
        (rec.id,),
    )
    known |= {str(row["occurrence_date"]) for row in await cursor.fetchall()}
    created = 0
    for day in wanted:
        if day.isoformat() in known:
            continue
        _task, was_created = await db.add_task(
            owner_id=rec.owner_id,
            title=rec.title,
            source_text=rec.title,
            task_date=day,
            due_at_utc=to_utc(day, rec.time_local, clock.tz) if rec.time_local else None,
            now=clock.now_utc(),
            source_chat_id=SYNTHETIC_CHAT,
            source_message_id=rec.id,
            source_line=day.toordinal(),
            category_id=rec.category_id,
            recurrence_id=rec.id,
            occurrence_date=day,
        )
        if was_created:
            created += 1
    return created


async def update_series(
    db: Database,
    clock: Clock,
    owner_id: int,
    rec_id: int,
    *,
    title: str | None = None,
    time_local=_UNSET,
    category_id=_UNSET,
) -> Recurrence | None:
    """Меняет шаблон и будущие открытые экземпляры, которые пользователь не менял отдельно."""

    rec = await get_recurrence(db, owner_id, rec_id)
    if rec is None:
        return None
    now = clock.now_utc()
    new_title = rec.title
    if title is not None:
        new_title = validate(title=title, kind=rec.kind, weekdays=rec.weekdays, month_day=rec.month_day)
    new_time = rec.time_local if time_local is _UNSET else time_local
    new_category = rec.category_id if category_id is _UNSET else category_id
    connection = db.conn
    await connection.execute(
        """
        UPDATE recurrences
        SET title = ?, time_local = ?, category_id = ?, updated_at = ?
        WHERE id = ? AND owner_id = ?
        """,
        (
            new_title,
            new_time.strftime("%H:%M") if new_time else None,
            new_category,
            dump_dt(now),
            rec_id,
            owner_id,
        ),
    )
    changed = await _future_open_ids(db, owner_id, rec_id, clock.today())
    for task_id, day in changed:
        due = to_utc(day, new_time, clock.tz) if new_time else None
        await connection.execute(
            """
            UPDATE tasks
            SET title = ?, source_text = ?, due_at_utc = ?, category_id = ?, updated_at = ?
            WHERE id = ? AND owner_id = ? AND status = 'open' AND detached = 0
            """,
            (new_title, new_title, dump_dt(due), new_category, dump_dt(now), task_id, owner_id),
        )
    await connection.commit()
    for task_id, _day in changed:
        await db.cancel_open_deliveries(
            owner_id,
            now=now,
            reason="rescheduled",
            kinds=("reminder", "snooze"),
            task_id=task_id,
        )
    return await get_recurrence(db, owner_id, rec_id)


@dataclass
class SchedulePlan:
    """Что изменится при сохранении нового шаблона. Ничего не пишет в базу."""

    update: int  # будущие открытые задачи, которые остаются и получают новые название и время
    remove: int  # будущие открытые задачи, которые не подходят под новое расписание
    create: int  # новые задачи на ближайшие 30 дней
    kept: int  # выполненные, прошлые и изменённые отдельно: не меняются


def _with(rec: Recurrence, *, title, kind, weekdays, month_day, time_local) -> Recurrence:
    cleaned = validate(title=title, kind=kind, weekdays=weekdays, month_day=month_day)
    return Recurrence(
        id=rec.id,
        owner_id=rec.owner_id,
        title=cleaned,
        kind=kind,
        weekdays=tuple(sorted(set(weekdays))) if kind == "weekly" else (),
        month_day=month_day if kind == "monthly" else None,
        start_date=rec.start_date,
        time_local=time_local,
        category_id=rec.category_id,
        active=rec.active,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


async def plan_template_change(
    db: Database,
    clock: Clock,
    rec: Recurrence,
    *,
    title: str,
    kind: str,
    weekdays: tuple[int, ...],
    month_day: int | None,
    time_local: time | None,
) -> SchedulePlan:
    new = _with(rec, title=title, kind=kind, weekdays=weekdays, month_day=month_day, time_local=time_local)
    today = clock.today()
    future = await _future_open_ids(db, rec.owner_id, rec.id, today)
    update = sum(1 for _tid, day in future if matches(new, day))
    remove = len(future) - update
    cursor = await db.conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE recurrence_id = ?", (rec.id,)
    )
    total = int((await cursor.fetchone())["n"])
    create = 0
    if rec.active:
        wanted = occurrence_dates(new, today, today + timedelta(days=HORIZON_DAYS - 1))
        cursor = await db.conn.execute(
            "SELECT occurrence_date FROM tasks WHERE recurrence_id = ?", (rec.id,)
        )
        known = {str(row["occurrence_date"]) for row in await cursor.fetchall()}
        removed_days = {day.isoformat() for _tid, day in future if not matches(new, day)}
        known -= removed_days
        cursor = await db.conn.execute(
            "SELECT occurrence_date FROM recurrence_exceptions WHERE recurrence_id = ?",
            (rec.id,),
        )
        known |= {str(row["occurrence_date"]) for row in await cursor.fetchall()}
        create = sum(1 for day in wanted if day.isoformat() not in known)
    return SchedulePlan(update=update, remove=remove, create=create, kept=total - len(future))


async def update_template(
    db: Database,
    clock: Clock,
    owner_id: int,
    rec_id: int,
    *,
    title: str,
    kind: str,
    weekdays: tuple[int, ...],
    month_day: int | None,
    time_local: time | None,
) -> Recurrence | None:
    """Меняет название и расписание шаблона и пересчитывает будущие открытые экземпляры.

    Выполненные, прошлые и изменённые отдельно задачи не затрагиваются. Категория
    и связь с заметкой остаются. Напоминания изменённых задач отменяются, новые
    создаёт обычный цикл планирования.
    """

    rec = await get_recurrence(db, owner_id, rec_id)
    if rec is None:
        return None
    new = _with(rec, title=title, kind=kind, weekdays=weekdays, month_day=month_day, time_local=time_local)
    now = clock.now_utc()
    connection = db.conn
    future = await _future_open_ids(db, owner_id, rec_id, clock.today())
    await connection.execute(
        """
        UPDATE recurrences
        SET title = ?, kind = ?, weekdays = ?, month_day = ?, time_local = ?, updated_at = ?
        WHERE id = ? AND owner_id = ?
        """,
        (
            new.title,
            new.kind,
            ",".join(str(day) for day in new.weekdays),
            new.month_day,
            new.time_local.strftime("%H:%M") if new.time_local else None,
            dump_dt(now),
            rec_id,
            owner_id,
        ),
    )
    removed: list[int] = []
    kept: list[int] = []
    for task_id, day in future:
        if matches(new, day):
            due = to_utc(day, new.time_local, clock.tz) if new.time_local else None
            await connection.execute(
                """
                UPDATE tasks
                SET title = ?, source_text = ?, due_at_utc = ?, updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = 'open' AND detached = 0
                """,
                (new.title, new.title, dump_dt(due), dump_dt(now), task_id, owner_id),
            )
            kept.append(task_id)
        else:
            await connection.execute(
                "DELETE FROM tasks WHERE id = ? AND owner_id = ? AND status = 'open' AND detached = 0",
                (task_id, owner_id),
            )
            removed.append(task_id)
    await connection.commit()
    for task_id in kept + removed:
        await db.cancel_open_deliveries(
            owner_id,
            now=now,
            reason="rescheduled",
            kinds=("reminder", "snooze"),
            task_id=task_id,
        )
    if new.active:
        await generate_for(db, clock, owner_id, rec_id)
    return await get_recurrence(db, owner_id, rec_id)


async def set_active(
    db: Database,
    clock: Clock,
    owner_id: int,
    rec_id: int,
    active: bool,
) -> Recurrence | None:
    rec = await get_recurrence(db, owner_id, rec_id)
    if rec is None:
        return None
    if rec.active == active:
        return rec
    await db.conn.execute(
        "UPDATE recurrences SET active = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
        (1 if active else 0, dump_dt(clock.now_utc()), rec_id, owner_id),
    )
    await db.conn.commit()
    if active:
        await generate_for(db, clock, owner_id, rec_id)
    else:
        await remove_future_instances(db, clock, owner_id, rec_id)
    return await get_recurrence(db, owner_id, rec_id)


async def delete_series(db: Database, clock: Clock, owner_id: int, rec_id: int) -> bool:
    rec = await get_recurrence(db, owner_id, rec_id)
    if rec is None:
        return False
    await remove_future_instances(db, clock, owner_id, rec_id)
    cursor = await db.conn.execute(
        "DELETE FROM recurrences WHERE id = ? AND owner_id = ?",
        (rec_id, owner_id),
    )
    await db.conn.commit()
    return cursor.rowcount == 1


async def remove_future_instances(
    db: Database,
    clock: Clock,
    owner_id: int,
    rec_id: int,
) -> int:
    """Удаляет будущие открытые экземпляры. Выполненные, прошлые и перенесённые остаются."""

    removed = 0
    for task_id, _day in await _future_open_ids(db, owner_id, rec_id, clock.today()):
        if await db.delete_task(owner_id, task_id, remember=False):
            removed += 1
    return removed


async def upcoming_dates(db: Database, rec: Recurrence, clock: Clock, count: int = 3) -> list[date]:
    found = occurrence_dates(rec, clock.today(), clock.today() + timedelta(days=800))
    return found[:count]


async def _future_open_ids(
    db: Database,
    owner_id: int,
    rec_id: int,
    today: date,
) -> list[tuple[int, date]]:
    cursor = await db.conn.execute(
        """
        SELECT id, occurrence_date FROM tasks
        WHERE owner_id = ? AND recurrence_id = ? AND status = 'open'
          AND detached = 0 AND occurrence_date >= ?
        ORDER BY occurrence_date ASC
        """,
        (owner_id, rec_id, today.isoformat()),
    )
    return [
        (int(row["id"]), date.fromisoformat(row["occurrence_date"]))
        for row in await cursor.fetchall()
    ]


def _rec(row) -> Recurrence:
    created = load_dt(row["created_at"])
    updated = load_dt(row["updated_at"])
    if created is None or updated is None:
        raise RuntimeError("В базе повреждены даты повторения.")
    weekdays = tuple(int(item) for item in str(row["weekdays"]).split(",") if item != "")
    raw_time = row["time_local"]
    return Recurrence(
        id=int(row["id"]),
        owner_id=int(row["owner_id"]),
        title=str(row["title"]),
        kind=str(row["kind"]),
        weekdays=weekdays,
        month_day=int(row["month_day"]) if row["month_day"] is not None else None,
        start_date=date.fromisoformat(row["start_date"]),
        time_local=(
            time(int(str(raw_time)[:2]), int(str(raw_time)[3:5])) if raw_time else None
        ),
        category_id=int(row["category_id"]) if row["category_id"] is not None else None,
        active=bool(row["active"]),
        created_at=created,
        updated_at=updated,
    )
