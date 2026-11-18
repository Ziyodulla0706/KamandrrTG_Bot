from dataclasses import dataclass
from datetime import datetime
from html import escape

from taskbot.dates import dump_dt, load_dt
from taskbot.db import Database

PART_LIMIT = 3500
LIST_SNIPPET = 48

FILTER_ALL = "all"
FILTER_PINNED = "pin"


@dataclass
class Note:
    id: int
    owner_id: int
    text: str
    category_id: int | None
    pinned: bool
    created_at: datetime
    updated_at: datetime


async def add_note(db: Database, owner_id: int, text: str, now: datetime) -> Note:
    stamp = dump_dt(now)
    cursor = await db.conn.execute(
        """
        INSERT INTO notes (owner_id, text, category_id, pinned, created_at, updated_at)
        VALUES (?, ?, NULL, 0, ?, ?)
        """,
        (owner_id, text, stamp, stamp),
    )
    await db.conn.commit()
    note = await get_note(db, owner_id, int(cursor.lastrowid))
    if note is None:
        raise RuntimeError("Заметка сохранилась, но не читается из базы.")
    return note


async def get_note(db: Database, owner_id: int, note_id: int) -> Note | None:
    cursor = await db.conn.execute(
        "SELECT * FROM notes WHERE id = ? AND owner_id = ?",
        (note_id, owner_id),
    )
    row = await cursor.fetchone()
    return _note(row) if row is not None else None


async def list_notes(db: Database, owner_id: int, flt: str = FILTER_ALL) -> list[Note]:
    """Фильтр: all, pin, c0 (без категории) или c<ID>."""

    clauses = ["owner_id = ?"]
    params: list[object] = [owner_id]
    if flt == FILTER_PINNED:
        clauses.append("pinned = 1")
    elif flt == "c0":
        clauses.append("category_id IS NULL")
    elif flt.startswith("c") and flt[1:].isdigit():
        clauses.append("category_id = ?")
        params.append(int(flt[1:]))
    cursor = await db.conn.execute(
        f"""
        SELECT * FROM notes WHERE {' AND '.join(clauses)}
        ORDER BY pinned DESC, updated_at DESC, id DESC
        """,
        tuple(params),
    )
    return [_note(row) for row in await cursor.fetchall()]


async def update_text(
    db: Database,
    owner_id: int,
    note_id: int,
    text: str,
    now: datetime,
) -> Note | None:
    cursor = await db.conn.execute(
        "UPDATE notes SET text = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
        (text, dump_dt(now), note_id, owner_id),
    )
    await db.conn.commit()
    return await get_note(db, owner_id, note_id) if cursor.rowcount == 1 else None


async def set_pinned(
    db: Database,
    owner_id: int,
    note_id: int,
    pinned: bool,
) -> Note | None:
    """Устанавливает признак явно, поэтому повторное нажатие ничего не переворачивает.

    Закрепление не считается правкой текста и не меняет дату обновления.
    """

    cursor = await db.conn.execute(
        "UPDATE notes SET pinned = ? WHERE id = ? AND owner_id = ?",
        (1 if pinned else 0, note_id, owner_id),
    )
    await db.conn.commit()
    return await get_note(db, owner_id, note_id) if cursor.rowcount == 1 else None


async def set_note_category(
    db: Database,
    owner_id: int,
    note_id: int,
    category_id: int | None,
    now: datetime,
) -> Note | None:
    cursor = await db.conn.execute(
        "UPDATE notes SET category_id = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
        (category_id, dump_dt(now), note_id, owner_id),
    )
    await db.conn.commit()
    return await get_note(db, owner_id, note_id) if cursor.rowcount == 1 else None


async def delete_note(db: Database, owner_id: int, note_id: int) -> bool:
    cursor = await db.conn.execute(
        "DELETE FROM notes WHERE id = ? AND owner_id = ?",
        (note_id, owner_id),
    )
    await db.conn.commit()
    return cursor.rowcount == 1


async def count_linked_tasks(db: Database, owner_id: int, note_id: int) -> int:
    cursor = await db.conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE owner_id = ? AND note_id = ?",
        (owner_id, note_id),
    )
    row = await cursor.fetchone()
    return int(row["n"])


def first_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            return cleaned
    return "(пусто)"


def shorten(text: str, limit: int = LIST_SNIPPET) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def list_title(note: Note) -> str:
    return shorten(first_line(note.text))


def split_parts(text: str, limit: int = PART_LIMIT) -> list[str]:
    """Делит текст на части так, чтобы после экранирования HTML каждая влезала в limit.

    Склейка частей даёт исходный текст без потерь.
    """

    if not text:
        return [""]
    pieces: list[str] = []
    for segment in text.splitlines(keepends=True):
        if len(escape(segment, quote=False)) <= limit:
            pieces.append(segment)
            continue
        buffer = ""
        for char in segment:
            if len(escape(buffer + char, quote=False)) > limit:
                pieces.append(buffer)
                buffer = ""
            buffer += char
        if buffer:
            pieces.append(buffer)
    parts: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(escape(current + piece, quote=False)) > limit:
            parts.append(current)
            current = ""
        current += piece
    if current:
        parts.append(current)
    return parts


def _note(row) -> Note:
    created = load_dt(row["created_at"])
    updated = load_dt(row["updated_at"])
    if created is None or updated is None:
        raise RuntimeError("В базе повреждены даты заметки.")
    return Note(
        id=int(row["id"]),
        owner_id=int(row["owner_id"]),
        text=str(row["text"]),
        category_id=int(row["category_id"]) if row["category_id"] is not None else None,
        pinned=bool(row["pinned"]),
        created_at=created,
        updated_at=updated,
    )
