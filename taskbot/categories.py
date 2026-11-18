from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from taskbot.dates import dump_dt
from taskbot.db import Database

DEFAULT_CATEGORIES = ("Работа", "Личное", "Идеи")
MAX_NAME = 32
NO_CATEGORY = "Без категории"


class CategoryError(Exception):
    """Понятная пользователю причина, почему название не подошло."""


@dataclass
class Category:
    id: int
    owner_id: int
    name: str


def normalize_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise CategoryError("Название категории не может быть пустым.")
    if len(name) > MAX_NAME:
        raise CategoryError(f"Название длиннее {MAX_NAME} символов. Сократите его.")
    return name


def name_key(name: str) -> str:
    # casefold корректно работает с кириллицей, в отличие от SQLite LOWER/NOCASE.
    return name.casefold()


async def seed_defaults(db: Database, owner_id: int, now: datetime) -> bool:
    """Создаёт «Работа», «Личное», «Идеи» один раз. Удалённые заново не появляются."""

    await db.ensure_settings(owner_id, now)
    connection = db.conn
    cursor = await connection.execute(
        """
        UPDATE user_settings SET categories_seeded = 1
        WHERE owner_id = ? AND categories_seeded = 0
        """,
        (owner_id,),
    )
    if cursor.rowcount != 1:
        await connection.rollback()
        return False
    for name in DEFAULT_CATEGORIES:
        await connection.execute(
            """
            INSERT OR IGNORE INTO categories (owner_id, name, name_key, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (owner_id, name, name_key(name), dump_dt(now)),
        )
    await connection.commit()
    return True


async def list_categories(db: Database, owner_id: int) -> list[Category]:
    cursor = await db.conn.execute(
        "SELECT id, owner_id, name FROM categories WHERE owner_id = ? ORDER BY id ASC",
        (owner_id,),
    )
    return [_row(row) for row in await cursor.fetchall()]


async def get_category(db: Database, owner_id: int, category_id: int) -> Category | None:
    cursor = await db.conn.execute(
        "SELECT id, owner_id, name FROM categories WHERE id = ? AND owner_id = ?",
        (category_id, owner_id),
    )
    row = await cursor.fetchone()
    return _row(row) if row is not None else None


async def category_names(db: Database, owner_id: int) -> dict[int, str]:
    return {item.id: item.name for item in await list_categories(db, owner_id)}


async def create_category(db: Database, owner_id: int, raw: str, now: datetime) -> Category:
    name = normalize_name(raw)
    try:
        cursor = await db.conn.execute(
            """
            INSERT INTO categories (owner_id, name, name_key, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (owner_id, name, name_key(name), dump_dt(now)),
        )
        await db.conn.commit()
    except aiosqlite.IntegrityError as exc:
        await db.conn.rollback()
        raise CategoryError("Такая категория уже есть.") from exc
    return Category(id=int(cursor.lastrowid), owner_id=owner_id, name=name)


async def rename_category(
    db: Database,
    owner_id: int,
    category_id: int,
    raw: str,
) -> Category | None:
    name = normalize_name(raw)
    try:
        cursor = await db.conn.execute(
            "UPDATE categories SET name = ?, name_key = ? WHERE id = ? AND owner_id = ?",
            (name, name_key(name), category_id, owner_id),
        )
        await db.conn.commit()
    except aiosqlite.IntegrityError as exc:
        await db.conn.rollback()
        raise CategoryError("Такая категория уже есть.") from exc
    if cursor.rowcount != 1:
        return None
    return Category(id=category_id, owner_id=owner_id, name=name)


async def delete_category(db: Database, owner_id: int, category_id: int) -> bool:
    """Записи сохраняются: внешние ключи обнуляют category_id."""

    cursor = await db.conn.execute(
        "DELETE FROM categories WHERE id = ? AND owner_id = ?",
        (category_id, owner_id),
    )
    await db.conn.commit()
    return cursor.rowcount == 1


async def usage(db: Database, owner_id: int, category_id: int) -> tuple[int, int, int]:
    """Сколько задач, заметок и повторений используют категорию."""

    counts: list[int] = []
    for table in ("tasks", "notes", "recurrences"):
        cursor = await db.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE owner_id = ? AND category_id = ?",
            (owner_id, category_id),
        )
        row = await cursor.fetchone()
        counts.append(int(row["n"]))
    return counts[0], counts[1], counts[2]


def _row(row) -> Category:
    return Category(id=int(row["id"]), owner_id=int(row["owner_id"]), name=str(row["name"]))
