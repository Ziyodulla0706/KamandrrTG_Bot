import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import aiosqlite

from taskbot.dates import dump_dt, load_dt, utc_now

SCHEMA_VERSION = 3
DELIVERY_KINDS = ("digest", "reminder", "snooze")
DELIVERY_STATUSES = ("pending", "processing", "sent", "cancelled", "skipped", "blocked")
REMINDER_LEADS = (0, 15, 30, 60)

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            source_text TEXT NOT NULL,
            task_date TEXT NOT NULL,
            due_at_utc TEXT,
            status TEXT NOT NULL CHECK (status IN ('open', 'done')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            source_chat_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            source_line INTEGER NOT NULL,
            UNIQUE (owner_id, source_chat_id, source_message_id, source_line)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_owner_status_date
            ON tasks (owner_id, status, task_date)
        """,
    ),
    2: (
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            owner_id INTEGER PRIMARY KEY,
            digest_enabled INTEGER NOT NULL DEFAULT 1 CHECK (digest_enabled IN (0, 1)),
            digest_time TEXT NOT NULL DEFAULT '09:00',
            reminders_enabled INTEGER NOT NULL DEFAULT 1 CHECK (reminders_enabled IN (0, 1)),
            reminder_lead_minutes INTEGER NOT NULL DEFAULT 30
                CHECK (reminder_lead_minutes IN (0, 15, 30, 60)),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('digest', 'reminder', 'snooze')),
            task_id INTEGER,
            due_key TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'processing', 'sent', 'cancelled', 'skipped', 'blocked')
            ),
            task_due_at_utc TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            locked_at TEXT,
            last_error TEXT,
            cancel_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sent_at TEXT,
            UNIQUE (owner_id, due_key)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_deliveries_due
            ON deliveries (owner_id, status, scheduled_for)
        """,
    ),
    3: (
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (owner_id, name_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            category_id INTEGER REFERENCES categories (id) ON DELETE SET NULL,
            pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS recurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('daily', 'weekdays', 'weekly', 'monthly')),
            weekdays TEXT NOT NULL DEFAULT '',
            month_day INTEGER,
            start_date TEXT NOT NULL,
            time_local TEXT,
            category_id INTEGER REFERENCES categories (id) ON DELETE SET NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS recurrence_exceptions (
            recurrence_id INTEGER NOT NULL REFERENCES recurrences (id) ON DELETE CASCADE,
            occurrence_date TEXT NOT NULL,
            PRIMARY KEY (recurrence_id, occurrence_date)
        )
        """,
    ),
}

# Колонки добавляются только если их ещё нет, поэтому прерванную миграцию можно повторить.
_V3_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("tasks", "category_id", "category_id INTEGER REFERENCES categories (id) ON DELETE SET NULL"),
    ("tasks", "note_id", "note_id INTEGER REFERENCES notes (id) ON DELETE SET NULL"),
    (
        "tasks",
        "recurrence_id",
        "recurrence_id INTEGER REFERENCES recurrences (id) ON DELETE SET NULL",
    ),
    ("tasks", "occurrence_date", "occurrence_date TEXT"),
    ("tasks", "detached", "detached INTEGER NOT NULL DEFAULT 0"),
    ("user_settings", "categories_seeded", "categories_seeded INTEGER NOT NULL DEFAULT 0"),
)
_V3_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_notes_owner ON notes (owner_id, pinned, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_notes_category ON notes (category_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks (category_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_note ON tasks (note_id)",
    "CREATE INDEX IF NOT EXISTS idx_recurrences_owner ON recurrences (owner_id, active)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_occurrence
        ON tasks (recurrence_id, occurrence_date) WHERE recurrence_id IS NOT NULL
    """,
)


@dataclass
class Task:
    id: int
    owner_id: int
    title: str
    source_text: str
    task_date: date
    due_at_utc: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    source_chat_id: int
    source_message_id: int
    source_line: int
    category_id: int | None = None
    note_id: int | None = None
    recurrence_id: int | None = None
    occurrence_date: date | None = None
    detached: bool = False


@dataclass
class Settings:
    owner_id: int
    digest_enabled: bool
    digest_time: time
    reminders_enabled: bool
    reminder_lead_minutes: int
    updated_at: datetime


@dataclass
class Delivery:
    id: int
    owner_id: int
    kind: str
    task_id: int | None
    due_key: str
    scheduled_for: datetime
    status: str
    task_due_at_utc: datetime | None
    attempts: int
    next_attempt_at: datetime | None
    locked_at: datetime | None
    last_error: str | None
    cancel_reason: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self.last_backup: Path | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        return self._require()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._conn.execute("PRAGMA journal_mode = WAL")

    async def close(self) -> None:
        connection = self._conn
        self._conn = None
        if connection is not None:
            await connection.close()

    async def migrate(self) -> None:
        connection = self._require()
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        await connection.commit()
        cursor = await connection.execute("SELECT MAX(version) AS version FROM schema_migrations")
        row = await cursor.fetchone()
        current = int(row["version"]) if row and row["version"] is not None else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"База использует схему версии {current}, а этот бот понимает только "
                f"до версии {SCHEMA_VERSION}. Обновите программу и не удаляйте файл базы."
            )
        if 0 < current < SCHEMA_VERSION:
            self.last_backup = await asyncio.to_thread(
                _backup_database,
                self.path,
                self.path.parent / "backups",
                current,
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS[version]:
                await connection.execute(statement)
            if version == 3:
                await _finish_v3(connection)
            await connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, dump_dt(utc_now())),
            )
            await connection.commit()

    async def schema_version(self) -> int:
        connection = self._require()
        cursor = await connection.execute("SELECT MAX(version) AS version FROM schema_migrations")
        row = await cursor.fetchone()
        if row is None or row["version"] is None:
            return 0
        return int(row["version"])

    async def add_task(
        self,
        *,
        owner_id: int,
        title: str,
        source_text: str,
        task_date: date,
        due_at_utc: datetime | None,
        now: datetime,
        source_chat_id: int,
        source_message_id: int,
        source_line: int,
        category_id: int | None = None,
        note_id: int | None = None,
        recurrence_id: int | None = None,
        occurrence_date: date | None = None,
    ) -> tuple[Task, bool]:
        connection = self._require()
        stamp = dump_dt(now)
        try:
            cursor = await connection.execute(
                """
                INSERT INTO tasks (
                    owner_id, title, source_text, task_date, due_at_utc, status,
                    created_at, updated_at, completed_at,
                    source_chat_id, source_message_id, source_line,
                    category_id, note_id, recurrence_id, occurrence_date
                ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    title,
                    source_text,
                    task_date.isoformat(),
                    dump_dt(due_at_utc),
                    stamp,
                    stamp,
                    source_chat_id,
                    source_message_id,
                    source_line,
                    category_id,
                    note_id,
                    recurrence_id,
                    occurrence_date.isoformat() if occurrence_date else None,
                ),
            )
            await connection.commit()
        except aiosqlite.IntegrityError:
            await connection.rollback()
            existing = await self.get_by_source(
                owner_id, source_chat_id, source_message_id, source_line
            )
            if existing is None:
                raise
            return existing, False
        task = await self.get_task(owner_id, int(cursor.lastrowid))
        if task is None:
            raise RuntimeError("Задача сохранилась, но не читается из базы.")
        return task, True

    async def get_task(self, owner_id: int, task_id: int) -> Task | None:
        connection = self._require()
        cursor = await connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
            (task_id, owner_id),
        )
        row = await cursor.fetchone()
        return _task_from_row(row) if row is not None else None

    async def get_by_source(
        self,
        owner_id: int,
        source_chat_id: int,
        source_message_id: int,
        source_line: int,
    ) -> Task | None:
        connection = self._require()
        cursor = await connection.execute(
            """
            SELECT * FROM tasks
            WHERE owner_id = ? AND source_chat_id = ?
              AND source_message_id = ? AND source_line = ?
            """,
            (owner_id, source_chat_id, source_message_id, source_line),
        )
        row = await cursor.fetchone()
        return _task_from_row(row) if row is not None else None

    async def list_tasks(self, owner_id: int, status: str) -> list[Task]:
        connection = self._require()
        cursor = await connection.execute(
            """
            SELECT * FROM tasks
            WHERE owner_id = ? AND status = ?
            ORDER BY task_date ASC,
                     CASE WHEN due_at_utc IS NULL THEN 1 ELSE 0 END,
                     due_at_utc ASC,
                     id ASC
            """,
            (owner_id, status),
        )
        rows = await cursor.fetchall()
        return [_task_from_row(row) for row in rows]

    async def list_all_tasks(self, owner_id: int) -> list[Task]:
        connection = self._require()
        cursor = await connection.execute(
            "SELECT * FROM tasks WHERE owner_id = ? ORDER BY id ASC",
            (owner_id,),
        )
        return [_task_from_row(row) for row in await cursor.fetchall()]

    async def set_status(
        self,
        owner_id: int,
        task_id: int,
        status: str,
        now: datetime,
    ) -> Task | None:
        completed = dump_dt(now) if status == "done" else None
        updated = await self._update(
            owner_id,
            task_id,
            """
            UPDATE tasks
            SET status = ?, completed_at = ?, updated_at = ?
            WHERE id = ? AND owner_id = ?
            """,
            (status, completed, dump_dt(now), task_id, owner_id),
        )
        if updated is not None and status == "done":
            await self.cancel_open_deliveries(
                owner_id,
                now=now,
                reason="task_closed",
                kinds=("reminder", "snooze"),
                task_id=task_id,
            )
        return updated

    async def set_title(
        self,
        owner_id: int,
        task_id: int,
        title: str,
        now: datetime,
    ) -> Task | None:
        return await self._update(
            owner_id,
            task_id,
            """
            UPDATE tasks
            SET title = ?, updated_at = ?,
                detached = CASE WHEN recurrence_id IS NULL THEN detached ELSE 1 END
            WHERE id = ? AND owner_id = ?
            """,
            (title, dump_dt(now), task_id, owner_id),
        )

    async def set_category(
        self,
        owner_id: int,
        task_id: int,
        category_id: int | None,
        now: datetime,
    ) -> Task | None:
        return await self._update(
            owner_id,
            task_id,
            """
            UPDATE tasks
            SET category_id = ?, updated_at = ?,
                detached = CASE WHEN recurrence_id IS NULL THEN detached ELSE 1 END
            WHERE id = ? AND owner_id = ?
            """,
            (category_id, dump_dt(now), task_id, owner_id),
        )

    async def set_schedule(
        self,
        owner_id: int,
        task_id: int,
        task_date: date,
        due_at_utc: datetime | None,
        now: datetime,
    ) -> Task | None:
        updated = await self._update(
            owner_id,
            task_id,
            """
            UPDATE tasks
            SET task_date = ?, due_at_utc = ?, updated_at = ?,
                detached = CASE WHEN recurrence_id IS NULL THEN detached ELSE 1 END
            WHERE id = ? AND owner_id = ?
            """,
            (task_date.isoformat(), dump_dt(due_at_utc), dump_dt(now), task_id, owner_id),
        )
        if updated is not None:
            await self.cancel_open_deliveries(
                owner_id,
                now=now,
                reason="rescheduled",
                kinds=("reminder", "snooze"),
                task_id=task_id,
            )
        return updated

    async def delete_task(self, owner_id: int, task_id: int, *, remember: bool = True) -> bool:
        """Удаляет задачу. Для повторяющейся запоминает дату, чтобы она не вернулась."""

        connection = self._require()
        existing = await self.get_task(owner_id, task_id)
        if existing is None:
            return False
        if remember and existing.recurrence_id is not None and existing.occurrence_date:
            await connection.execute(
                """
                INSERT OR IGNORE INTO recurrence_exceptions (recurrence_id, occurrence_date)
                VALUES (?, ?)
                """,
                (existing.recurrence_id, existing.occurrence_date.isoformat()),
            )
        cursor = await connection.execute(
            "DELETE FROM tasks WHERE id = ? AND owner_id = ?",
            (task_id, owner_id),
        )
        await connection.commit()
        deleted = cursor.rowcount == 1
        if deleted:
            await self.cancel_open_deliveries(
                owner_id,
                now=utc_now(),
                reason="task_deleted",
                kinds=("reminder", "snooze"),
                task_id=task_id,
            )
        return deleted

    async def ensure_settings(self, owner_id: int, now: datetime) -> Settings:
        connection = self._require()
        await connection.execute(
            """
            INSERT OR IGNORE INTO user_settings (
                owner_id, digest_enabled, digest_time, reminders_enabled,
                reminder_lead_minutes, updated_at
            ) VALUES (?, 1, '09:00', 1, 30, ?)
            """,
            (owner_id, dump_dt(now)),
        )
        await connection.commit()
        settings = await self.get_settings(owner_id)
        if settings is None:
            raise RuntimeError("Не удалось создать настройки пользователя.")
        return settings

    async def get_settings(self, owner_id: int) -> Settings | None:
        connection = self._require()
        cursor = await connection.execute(
            "SELECT * FROM user_settings WHERE owner_id = ?",
            (owner_id,),
        )
        row = await cursor.fetchone()
        return _settings_from_row(row) if row is not None else None

    async def save_settings(self, settings: Settings, now: datetime) -> Settings:
        if settings.reminder_lead_minutes not in REMINDER_LEADS:
            raise ValueError("Недопустимый интервал напоминания.")
        stamp = settings.digest_time.strftime("%H:%M")
        connection = self._require()
        cursor = await connection.execute(
            """
            UPDATE user_settings
            SET digest_enabled = ?, digest_time = ?, reminders_enabled = ?,
                reminder_lead_minutes = ?, updated_at = ?
            WHERE owner_id = ?
            """,
            (
                1 if settings.digest_enabled else 0,
                stamp,
                1 if settings.reminders_enabled else 0,
                settings.reminder_lead_minutes,
                dump_dt(now),
                settings.owner_id,
            ),
        )
        await connection.commit()
        if cursor.rowcount == 0:
            raise RuntimeError("Настройки пользователя не найдены.")
        stored = await self.get_settings(settings.owner_id)
        if stored is None:
            raise RuntimeError("Настройки сохранились, но не читаются из базы.")
        return stored

    async def insert_delivery(
        self,
        *,
        owner_id: int,
        kind: str,
        due_key: str,
        scheduled_for: datetime,
        now: datetime,
        task_id: int | None = None,
        task_due_at_utc: datetime | None = None,
        status: str = "pending",
        cancel_reason: str | None = None,
    ) -> tuple[Delivery, bool]:
        if kind not in DELIVERY_KINDS or status not in DELIVERY_STATUSES:
            raise ValueError("Недопустимый вид или статус отправки.")
        connection = self._require()
        stamp = dump_dt(now)
        cursor = await connection.execute(
            """
            INSERT OR IGNORE INTO deliveries (
                owner_id, kind, task_id, due_key, scheduled_for, status,
                task_due_at_utc, attempts, next_attempt_at, locked_at,
                last_error, cancel_reason, created_at, updated_at, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, ?, ?, ?, NULL)
            """,
            (
                owner_id,
                kind,
                task_id,
                due_key,
                dump_dt(scheduled_for),
                status,
                dump_dt(task_due_at_utc),
                cancel_reason,
                stamp,
                stamp,
            ),
        )
        created = cursor.rowcount == 1
        await connection.commit()
        delivery = await self.get_delivery_by_key(owner_id, due_key)
        if delivery is None:
            raise RuntimeError("Не удалось записать отправку.")
        return delivery, created

    async def get_delivery(self, owner_id: int, delivery_id: int) -> Delivery | None:
        connection = self._require()
        cursor = await connection.execute(
            "SELECT * FROM deliveries WHERE id = ? AND owner_id = ?",
            (delivery_id, owner_id),
        )
        row = await cursor.fetchone()
        return _delivery_from_row(row) if row is not None else None

    async def get_delivery_by_key(self, owner_id: int, due_key: str) -> Delivery | None:
        connection = self._require()
        cursor = await connection.execute(
            "SELECT * FROM deliveries WHERE owner_id = ? AND due_key = ?",
            (owner_id, due_key),
        )
        row = await cursor.fetchone()
        return _delivery_from_row(row) if row is not None else None

    async def list_deliveries(
        self,
        owner_id: int,
        *,
        statuses: tuple[str, ...] | None = None,
        kinds: tuple[str, ...] | None = None,
        task_id: int | None = None,
    ) -> list[Delivery]:
        clauses = ["owner_id = ?"]
        params: list[object] = [owner_id]
        if statuses:
            marks = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({marks})")
            params.extend(statuses)
        if kinds:
            marks = ", ".join("?" for _ in kinds)
            clauses.append(f"kind IN ({marks})")
            params.extend(kinds)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        connection = self._require()
        cursor = await connection.execute(
            f"SELECT * FROM deliveries WHERE {' AND '.join(clauses)} ORDER BY id ASC",
            tuple(params),
        )
        rows = await cursor.fetchall()
        return [_delivery_from_row(row) for row in rows]

    async def recover_stuck(self, owner_id: int, now: datetime, stuck_after: timedelta) -> int:
        connection = self._require()
        cursor = await connection.execute(
            """
            UPDATE deliveries
            SET status = 'pending', locked_at = NULL, updated_at = ?
            WHERE owner_id = ? AND status = 'processing'
              AND (locked_at IS NULL OR locked_at <= ?)
            """,
            (dump_dt(now), owner_id, dump_dt(now - stuck_after)),
        )
        await connection.commit()
        return int(cursor.rowcount)

    async def claim_due(self, owner_id: int, now: datetime) -> list[Delivery]:
        connection = self._require()
        now_s = dump_dt(now)
        cursor = await connection.execute(
            """
            SELECT id FROM deliveries
            WHERE owner_id = ? AND status = 'pending'
              AND scheduled_for <= ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY scheduled_for ASC, id ASC
            """,
            (owner_id, now_s, now_s),
        )
        ids = [int(row["id"]) for row in await cursor.fetchall()]
        claimed: list[int] = []
        for delivery_id in ids:
            updated = await connection.execute(
                """
                UPDATE deliveries
                SET status = 'processing', locked_at = ?, updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = 'pending'
                """,
                (now_s, now_s, delivery_id, owner_id),
            )
            if updated.rowcount == 1:
                claimed.append(delivery_id)
        await connection.commit()
        items: list[Delivery] = []
        for delivery_id in claimed:
            delivery = await self.get_delivery(owner_id, delivery_id)
            if delivery is not None:
                items.append(delivery)
        return items

    async def cancel_open_deliveries(
        self,
        owner_id: int,
        *,
        now: datetime,
        reason: str,
        kinds: tuple[str, ...] | None = None,
        task_id: int | None = None,
    ) -> int:
        clauses = ["owner_id = ?", "status IN ('pending', 'processing')"]
        where: list[object] = [owner_id]
        if kinds:
            marks = ", ".join("?" for _ in kinds)
            clauses.append(f"kind IN ({marks})")
            where.extend(kinds)
        if task_id is not None:
            clauses.append("task_id = ?")
            where.append(task_id)
        connection = self._require()
        cursor = await connection.execute(
            f"""
            UPDATE deliveries
            SET status = 'cancelled', cancel_reason = ?, locked_at = NULL,
                next_attempt_at = NULL, updated_at = ?
            WHERE {' AND '.join(clauses)}
            """,
            (reason, dump_dt(now), *where),
        )
        await connection.commit()
        return int(cursor.rowcount)

    async def reschedule_pending(
        self,
        owner_id: int,
        delivery_id: int,
        scheduled_for: datetime,
        now: datetime,
    ) -> bool:
        connection = self._require()
        cursor = await connection.execute(
            """
            UPDATE deliveries
            SET scheduled_for = ?, updated_at = ?
            WHERE id = ? AND owner_id = ? AND status = 'pending'
            """,
            (dump_dt(scheduled_for), dump_dt(now), delivery_id, owner_id),
        )
        await connection.commit()
        return cursor.rowcount == 1

    async def reopen_delivery(
        self,
        owner_id: int,
        delivery_id: int,
        *,
        scheduled_for: datetime,
        task_due_at_utc: datetime | None,
        now: datetime,
        only_statuses: tuple[str, ...],
    ) -> bool:
        if not only_statuses:
            return False
        marks = ", ".join("?" for _ in only_statuses)
        connection = self._require()
        cursor = await connection.execute(
            f"""
            UPDATE deliveries
            SET status = 'pending', scheduled_for = ?, task_due_at_utc = ?,
                attempts = 0, next_attempt_at = NULL, locked_at = NULL,
                last_error = NULL, cancel_reason = NULL, sent_at = NULL, updated_at = ?
            WHERE id = ? AND owner_id = ? AND status IN ({marks})
            """,
            (
                dump_dt(scheduled_for),
                dump_dt(task_due_at_utc),
                dump_dt(now),
                delivery_id,
                owner_id,
                *only_statuses,
            ),
        )
        await connection.commit()
        return cursor.rowcount == 1

    async def close_unsent(
        self,
        owner_id: int,
        delivery_id: int,
        *,
        now: datetime,
        status: str,
        reason: str,
        only_statuses: tuple[str, ...],
    ) -> bool:
        if status not in {"cancelled", "skipped"} or not only_statuses:
            raise ValueError("Так можно только отменить или пропустить отправку.")
        marks = ", ".join("?" for _ in only_statuses)
        connection = self._require()
        cursor = await connection.execute(
            f"""
            UPDATE deliveries
            SET status = ?, cancel_reason = ?, locked_at = NULL,
                next_attempt_at = NULL, updated_at = ?
            WHERE id = ? AND owner_id = ? AND status IN ({marks})
            """,
            (status, reason, dump_dt(now), delivery_id, owner_id, *only_statuses),
        )
        await connection.commit()
        return cursor.rowcount == 1

    async def settle_processing(
        self,
        owner_id: int,
        delivery_id: int,
        *,
        now: datetime,
        status: str,
        attempts: int,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
        cancel_reason: str | None = None,
        sent: bool = False,
    ) -> bool:
        if status not in DELIVERY_STATUSES:
            raise ValueError("Недопустимый статус отправки.")
        connection = self._require()
        cursor = await connection.execute(
            """
            UPDATE deliveries
            SET status = ?, attempts = ?, next_attempt_at = ?, locked_at = NULL,
                last_error = ?, cancel_reason = ?, sent_at = ?, updated_at = ?
            WHERE id = ? AND owner_id = ? AND status = 'processing'
            """,
            (
                status,
                attempts,
                dump_dt(next_attempt_at),
                last_error,
                cancel_reason,
                dump_dt(now) if sent else None,
                dump_dt(now),
                delivery_id,
                owner_id,
            ),
        )
        await connection.commit()
        return cursor.rowcount == 1

    async def _update(
        self,
        owner_id: int,
        task_id: int,
        sql: str,
        params: tuple,
    ) -> Task | None:
        connection = self._require()
        cursor = await connection.execute(sql, params)
        await connection.commit()
        if cursor.rowcount == 0:
            return None
        return await self.get_task(owner_id, task_id)

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Соединение с базой закрыто.")
        return self._conn


def _backup_database(source: Path, folder: Path, version: int) -> Path:
    """Согласованная копия средствами SQLite backup, безопасная при режиме WAL."""

    folder.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    target = folder / f"{source.stem}-schema{version}-{stamp}.sqlite3"
    origin = sqlite3.connect(source)
    copy = sqlite3.connect(target)
    try:
        origin.backup(copy)
        result = copy.execute("PRAGMA integrity_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise RuntimeError("Резервная копия базы не прошла проверку целостности.")
    finally:
        copy.close()
        origin.close()
    return target


async def _finish_v3(connection: aiosqlite.Connection) -> None:
    for table, column, ddl in _V3_COLUMNS:
        cursor = await connection.execute(f"PRAGMA table_info({table})")
        names = {str(row["name"]) for row in await cursor.fetchall()}
        if column not in names:
            await connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    for statement in _V3_INDEXES:
        await connection.execute(statement)


def _settings_from_row(row: aiosqlite.Row) -> Settings:
    updated = load_dt(row["updated_at"])
    if updated is None:
        raise RuntimeError("В базе повреждена дата настроек.")
    try:
        digest_time = datetime.strptime(str(row["digest_time"]), "%H:%M").time()
    except ValueError as exc:
        raise RuntimeError("В базе повреждено время сводки.") from exc
    lead = int(row["reminder_lead_minutes"])
    if lead not in REMINDER_LEADS:
        raise RuntimeError("В базе повреждён интервал напоминания.")
    return Settings(
        owner_id=int(row["owner_id"]),
        digest_enabled=bool(row["digest_enabled"]),
        digest_time=digest_time,
        reminders_enabled=bool(row["reminders_enabled"]),
        reminder_lead_minutes=lead,
        updated_at=updated,
    )


def _delivery_from_row(row: aiosqlite.Row) -> Delivery:
    scheduled = load_dt(row["scheduled_for"])
    created = load_dt(row["created_at"])
    updated = load_dt(row["updated_at"])
    if scheduled is None or created is None or updated is None:
        raise RuntimeError("В базе повреждены даты отправки.")
    task_id = row["task_id"]
    return Delivery(
        id=int(row["id"]),
        owner_id=int(row["owner_id"]),
        kind=str(row["kind"]),
        task_id=int(task_id) if task_id is not None else None,
        due_key=str(row["due_key"]),
        scheduled_for=scheduled,
        status=str(row["status"]),
        task_due_at_utc=load_dt(row["task_due_at_utc"]),
        attempts=int(row["attempts"]),
        next_attempt_at=load_dt(row["next_attempt_at"]),
        locked_at=load_dt(row["locked_at"]),
        last_error=row["last_error"],
        cancel_reason=row["cancel_reason"],
        created_at=created,
        updated_at=updated,
        sent_at=load_dt(row["sent_at"]),
    )


def _task_from_row(row: aiosqlite.Row) -> Task:
    created = load_dt(row["created_at"])
    updated = load_dt(row["updated_at"])
    if created is None or updated is None:
        raise RuntimeError("В базе повреждены служебные даты задачи.")
    return Task(
        id=int(row["id"]),
        owner_id=int(row["owner_id"]),
        title=str(row["title"]),
        source_text=str(row["source_text"]),
        task_date=date.fromisoformat(row["task_date"]),
        due_at_utc=load_dt(row["due_at_utc"]),
        status=str(row["status"]),
        created_at=created,
        updated_at=updated,
        completed_at=load_dt(row["completed_at"]),
        source_chat_id=int(row["source_chat_id"]),
        source_message_id=int(row["source_message_id"]),
        source_line=int(row["source_line"]),
        category_id=_opt_int(row["category_id"]),
        note_id=_opt_int(row["note_id"]),
        recurrence_id=_opt_int(row["recurrence_id"]),
        occurrence_date=(
            date.fromisoformat(row["occurrence_date"]) if row["occurrence_date"] else None
        ),
        detached=bool(row["detached"]),
    )


def _opt_int(value) -> int | None:
    return int(value) if value is not None else None
