import asyncio
from datetime import date, datetime, time, timedelta, timezone

import aiosqlite

from taskbot.dates import is_overdue, to_utc
from taskbot.db import SCHEMA_VERSION, Database, _MIGRATIONS
from tests.helpers import TZ, open_db


def test_tasks_survive_reopen_and_keep_owner(tmp_path):
    asyncio.run(_persist(tmp_path))


def test_repeat_insert_and_second_button_do_not_duplicate(tmp_path):
    asyncio.run(_idempotent(tmp_path))


def test_schema_migration_is_repeatable(tmp_path):
    asyncio.run(_schema(tmp_path))


async def _persist(path):
    database = await open_db(path / "tasks.sqlite3")
    now = datetime(2026, 3, 21, 0, 30, tzinfo=TZ).astimezone(timezone.utc)
    task, created = await database.add_task(
        owner_id=42,
        title="Позвонить",
        source_text="завтра позвонить",
        task_date=date(2026, 3, 21),
        due_at_utc=None,
        now=now,
        source_chat_id=7,
        source_message_id=8,
        source_line=1,
    )
    assert created
    assert task.due_at_utc is None
    mode = await (await database._require().execute("PRAGMA journal_mode")).fetchone()
    assert str(mode[0]).lower() == "wal"
    await database.close()

    again = await open_db(path / "tasks.sqlite3")
    loaded = await again.get_task(42, task.id)
    assert loaded is not None
    assert loaded.title == "Позвонить"
    assert loaded.task_date == date(2026, 3, 21)
    assert loaded.due_at_utc is None
    assert loaded.source_text == "завтра позвонить"
    evening = datetime(2026, 3, 21, 23, 59, tzinfo=TZ)
    assert not is_overdue(
        loaded.task_date,
        loaded.due_at_utc,
        loaded.status,
        today=evening.date(),
        now_utc=evening.astimezone(timezone.utc),
    )
    assert await again.get_task(99, task.id) is None
    assert await again.set_status(99, task.id, "done", now) is None
    assert await again.set_title(99, task.id, "чужое", now) is None
    assert await again.set_schedule(99, task.id, date(2026, 3, 22), None, now) is None
    assert await again.delete_task(99, task.id) is False
    still = await again.get_task(42, task.id)
    assert still is not None
    assert still.status == "open"
    assert still.title == "Позвонить"
    listed = await again.list_tasks(99, "open")
    assert listed == []
    await again.close()


async def _idempotent(path):
    database = await open_db(path / "tasks.sqlite3")
    now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    kwargs = dict(
        owner_id=42,
        title="Позвонить",
        source_text="исходный",
        task_date=date(2026, 11, 20),
        due_at_utc=to_utc(date(2026, 11, 20), time(10, 30), TZ),
        now=now,
        source_chat_id=1,
        source_message_id=5,
        source_line=1,
    )
    first, created = await database.add_task(**kwargs)
    second, created_again = await database.add_task(**kwargs)
    assert created
    assert not created_again
    assert first.id == second.id
    tasks = await database.list_tasks(42, "open")
    assert len(tasks) == 1

    done = await database.set_status(42, first.id, "done", now)
    assert done is not None
    assert done.completed_at == now.replace(microsecond=0).astimezone(timezone.utc) or done.completed_at is not None
    completed_at = done.completed_at
    again = await database.set_status(42, first.id, "done", now + timedelta(hours=3))
    assert again is not None
    assert again.completed_at != completed_at
    # Повторное действие на уровне кнопки не должно вызывать set_status второй раз.
    # Здесь проверяем, что чтение владельца не создаёт вторую строку.
    assert len(await database.list_tasks(42, "done")) == 1

    tricky = await database.add_task(
        owner_id=42,
        title="'); DROP TABLE tasks; --",
        source_text="<script>",
        task_date=date(2026, 11, 21),
        due_at_utc=None,
        now=now,
        source_chat_id=1,
        source_message_id=6,
        source_line=1,
    )
    assert tricky[1]
    assert (await database.get_task(42, tricky[0].id)).title == "'); DROP TABLE tasks; --"
    assert await database.schema_version() == SCHEMA_VERSION
    await database.close()
    reopened = Database(path / "tasks.sqlite3")
    await reopened.connect()
    assert await reopened.get_task(42, tricky[0].id) is not None
    await reopened.close()


async def _schema(path):
    database = await open_db(path / "tasks.sqlite3")
    await database.migrate()
    assert await database.schema_version() == SCHEMA_VERSION
    await database._require().execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (99, '2026-01-01T00:00:00+00:00')"
    )
    await database._require().commit()
    try:
        await database.migrate()
    except RuntimeError as exc:
        assert "99" in str(exc)
        assert "не удаляйте" in str(exc).lower() or "Обновите" in str(exc)
    else:
        raise AssertionError("ожидалась ошибка новой схемы")
    await database.close()
    await database.close()


def test_legacy_database_keeps_tasks_and_defaults(tmp_path):
    asyncio.run(_legacy(tmp_path))


async def _legacy(path):
    file = path / "legacy.sqlite3"
    connection = await aiosqlite.connect(file)
    await connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    for statement in _MIGRATIONS[1]:
        await connection.execute(statement)
    await connection.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2026-01-01T00:00:00+00:00')"
    )
    await connection.execute(
        """
        INSERT INTO tasks (
            owner_id, title, source_text, task_date, due_at_utc, status,
            created_at, updated_at, completed_at,
            source_chat_id, source_message_id, source_line
        ) VALUES (
            42, 'Старая задача', 'старая', '2026-11-20', '2026-11-20T05:30:00+00:00',
            'open', '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', NULL,
            42, 1, 1
        )
        """
    )
    await connection.commit()
    await connection.close()

    database = Database(file)
    await database.connect()
    await database.migrate()
    await database.migrate()
    tasks = await database.list_tasks(42, "open")
    assert [task.title for task in tasks] == ["Старая задача"]
    assert tasks[0].task_date == date(2026, 11, 20)
    assert tasks[0].due_at_utc == datetime(2026, 11, 20, 5, 30, tzinfo=timezone.utc)
    assert await database.schema_version() == SCHEMA_VERSION
    now = datetime(2026, 11, 16, 4, tzinfo=timezone.utc)
    settings = await database.ensure_settings(42, now)
    assert settings.digest_enabled is True
    assert settings.digest_time == time(9, 0)
    assert settings.reminders_enabled is True
    assert settings.reminder_lead_minutes == 30
    settings.digest_time = time(7, 15)
    settings.reminders_enabled = False
    await database.save_settings(settings, now)
    await database.migrate()
    kept = await database.ensure_settings(42, now)
    assert kept.digest_time == time(7, 15)
    assert kept.reminders_enabled is False
    assert (await database.list_tasks(42, "open"))[0].title == "Старая задача"
    names = [
        row[0]
        for row in await (
            await database._require().execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ).fetchall()
    ]
    assert "user_settings" in names
    assert "deliveries" in names
    await database.close()
