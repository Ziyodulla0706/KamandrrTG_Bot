"""Экспорт данных. Файлы собираются из одного согласованного снимка базы.

В выгрузку попадают только задачи, заметки и описание активных повторений.
Токен, .env, журналы и файл SQLite не включаются.
"""

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from taskbot.categories import category_names
from taskbot.dates import Clock, from_utc
from taskbot.db import Database, Task
from taskbot.notes import Note, list_notes
from taskbot.recurrence import Recurrence, describe_schedule, first_date, list_recurrences

CSV_HEADER = (
    "ID",
    "Название",
    "Статус",
    "Дата",
    "Время",
    "Часовой пояс",
    "Категория",
    "Повторяющаяся",
    "Создана",
    "Завершена",
)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
RULE = "=" * 32
THIN = "-" * 32


@dataclass
class Snapshot:
    tasks: list[Task]
    notes: list[Note]
    recurrences: list[Recurrence]
    categories: dict[int, str]
    tz: ZoneInfo
    today: date
    taken_at: datetime


async def take_snapshot(db: Database, owner_id: int, clock: Clock) -> Snapshot:
    """Читает все таблицы в одной транзакции, чтобы данные не расходились между файлами."""

    connection = db.conn
    began = False
    if not connection.in_transaction:
        await connection.execute("BEGIN")
        began = True
    try:
        tasks = await db.list_all_tasks(owner_id)
        notes = await list_notes(db, owner_id)
        recs = await list_recurrences(db, owner_id)
        names = await category_names(db, owner_id)
    finally:
        if began:
            await connection.rollback()
    return Snapshot(
        tasks=tasks,
        notes=notes,
        recurrences=recs,
        categories=names,
        tz=clock.tz,
        today=clock.today(),
        taken_at=clock.now(),
    )


def safe_cell(value: str) -> str:
    """Защита от формул: ячейка, начинающаяся с = + - @, получает апостроф."""

    if value and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def _local(moment: datetime | None, tz: ZoneInfo) -> str:
    if moment is None:
        return ""
    return from_utc(moment, tz).strftime("%Y-%m-%d %H:%M")


def tasks_csv(snapshot: Snapshot) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_HEADER)
    for task in snapshot.tasks:
        category = snapshot.categories.get(task.category_id or -1, "")
        due = (
            from_utc(task.due_at_utc, snapshot.tz).strftime("%H:%M")
            if task.due_at_utc is not None
            else ""
        )
        writer.writerow(
            [
                task.id,
                safe_cell(task.title),
                "выполнена" if task.status == "done" else "открыта",
                task.task_date.isoformat(),
                due,
                snapshot.tz.key,
                safe_cell(category),
                "да" if task.recurrence_id is not None else "нет",
                _local(task.created_at, snapshot.tz),
                _local(task.completed_at, snapshot.tz),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def notes_txt(snapshot: Snapshot) -> bytes:
    lines = [
        f"Заметки: {len(snapshot.notes)}",
        f"Выгрузка: {snapshot.taken_at.strftime('%Y-%m-%d %H:%M')} ({snapshot.tz.key})",
    ]
    for note in snapshot.notes:
        lines.extend(
            [
                "",
                RULE,
                f"Заметка №{note.id}",
                f"ID: {note.id}",
                f"Категория: {snapshot.categories.get(note.category_id or -1, 'Без категории')}",
                f"Закреплена: {'да' if note.pinned else 'нет'}",
                f"Создана: {_local(note.created_at, snapshot.tz)}",
                f"Обновлена: {_local(note.updated_at, snapshot.tz)}",
                THIN,
                note.text,
            ]
        )
    if snapshot.notes:
        lines.extend(["", RULE])
    return ("\n".join(lines) + "\n").encode("utf-8")


def recurrences_txt(snapshot: Snapshot) -> bytes:
    active = [rec for rec in snapshot.recurrences if rec.active]
    lines = [f"Активные повторяющиеся задачи: {len(active)}"]
    if not active:
        lines.append("Активных расписаний нет.")
    for rec in active:
        nxt = first_date(rec, snapshot.today)
        category = snapshot.categories.get(rec.category_id or -1, "Без категории")
        lines.extend(
            [
                "",
                RULE,
                f"{rec.title}",
                f"Расписание: {describe_schedule(rec)}",
                f"Начало: {rec.start_date.strftime('%d.%m.%Y')}",
                f"Ближайшая дата: {nxt.strftime('%d.%m.%Y') if nxt else 'нет'}",
                f"Категория: {category}",
                f"Часовой пояс: {snapshot.tz.key}",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def export_zip(snapshot: Snapshot) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("tasks.csv", tasks_csv(snapshot))
        archive.writestr("notes.txt", notes_txt(snapshot))
        archive.writestr("recurrences.txt", recurrences_txt(snapshot))
    return buffer.getvalue()


def write_export(kind: str, snapshot: Snapshot, directory: Path) -> Path:
    stamp = snapshot.taken_at.strftime("%Y%m%d-%H%M")
    if kind == "csv":
        path = directory / f"tasks-{stamp}.csv"
        path.write_bytes(tasks_csv(snapshot))
    elif kind == "txt":
        path = directory / f"notes-{stamp}.txt"
        path.write_bytes(notes_txt(snapshot))
    elif kind == "zip":
        path = directory / f"export-{stamp}.zip"
        path.write_bytes(export_zip(snapshot))
    else:
        raise ValueError("Неизвестный вид экспорта.")
    return path
