import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from taskbot.dates import Clock, dump_dt, to_utc
from taskbot.db import Database
from taskbot.handlers import setup_router
from taskbot.handlers.card import on_delete_yes, on_done, on_move_start
from taskbot.handlers.settings import on_digest_time_text, on_lead, on_settings, on_toggle_digest
from taskbot.handlers.snooze import on_snooze
from taskbot.handlers.states import SettingsDialog
from taskbot.keyboards import main_menu
from taskbot.schedule import run_tick
from taskbot.texts import BTN_SETTINGS
from taskbot.worker import notification_worker, poll_with_worker
from tests.helpers import TZ, FakeCallback, FakeMessage, FakeState, button_rows, clock_at, make_config, new_locks, open_db


def test_digest_uses_tashkent_not_process_utc(tmp_path):
    asyncio.run(_digest_uses_tashkent_not_process_utc(tmp_path))


def test_missed_digest_window(tmp_path):
    asyncio.run(_missed_digest_window(tmp_path))


def test_empty_digest_is_recorded_without_message(tmp_path):
    asyncio.run(_empty_digest_is_recorded_without_message(tmp_path))


def test_digest_compacts_a_long_list(tmp_path):
    asyncio.run(_digest_compacts_a_long_list(tmp_path))


def test_local_midnight_digest_keeps_tashkent_date(tmp_path):
    asyncio.run(_local_midnight_digest_keeps_tashkent_date(tmp_path))


def test_regular_reminder_and_near_due(tmp_path):
    asyncio.run(_regular_reminder_and_near_due(tmp_path))


def test_missed_reminder_grace(tmp_path):
    asyncio.run(_missed_reminder_grace(tmp_path))


def test_downtime_sends_reminder_while_due_is_ahead(tmp_path):
    asyncio.run(_downtime_sends_reminder_while_due_is_ahead(tmp_path))


def test_no_reminder_for_timeless_or_already_overdue(tmp_path):
    asyncio.run(_no_reminder_for_timeless_or_already_overdue(tmp_path))


def test_task_changes_cancel_pending_reminder(tmp_path):
    asyncio.run(_task_changes_cancel_pending_reminder(tmp_path))


def test_snooze_persists_and_ignores_repeat(tmp_path):
    asyncio.run(_snooze_persists_and_ignores_repeat(tmp_path))


def test_snooze_can_be_after_due(tmp_path):
    asyncio.run(_snooze_can_be_after_due(tmp_path))


def test_settings_change_lead_and_toggles(tmp_path):
    asyncio.run(_settings_change_lead_and_toggles(tmp_path))


def test_temporary_error_waits_before_retry(tmp_path):
    asyncio.run(_temporary_error_waits_before_retry(tmp_path))


def test_retry_after_uses_given_delay(tmp_path):
    asyncio.run(_retry_after_uses_given_delay(tmp_path))


def test_forbidden_stops_without_token(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    asyncio.run(_forbidden_stops_without_token(tmp_path))
    assert any("не выполнена" in record.message for record in caplog.records)
    assert "123456789:" not in caplog.text


def test_repeated_failures_stop(tmp_path):
    asyncio.run(_repeated_failures_stop(tmp_path))


def test_stuck_processing_is_recovered(tmp_path):
    asyncio.run(_stuck_processing_is_recovered(tmp_path))


def test_worker_stops_with_the_bot():
    asyncio.run(_worker_stops_with_the_bot())


def test_settings_router_is_before_free_text():
    names = [router.name for router in setup_router().sub_routers]
    assert names.index("settings") < names.index("intake")
    assert names.index("snooze") < names.index("fallback")
    labels = [button.text for row in main_menu().keyboard for button in row]
    assert BTN_SETTINGS in labels


class FakeSender:
    def __init__(self):
        self.messages = []
        self.errors = []
        self.calls = 0

    async def send(self, chat_id, text, reply_markup=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        self.messages.append({"chat_id": chat_id, "text": text, "markup": reply_markup})


async def _digest_uses_tashkent_not_process_utc(path):
    db, config, sender = await _world(path / "morning.sqlite3")
    early = clock_at(2026, 11, 16, 8, 0)
    day = early.today()
    await _add(db, early, "Созвон <b>", day, to_utc(day, time(15, 0), TZ), 1)
    await _add(db, early, "Без часа", day, None, 2)
    await _add(db, early, "Уже было", day, to_utc(day, time(8, 0), TZ), 3)
    yesterday = day - timedelta(days=1)
    await _add(db, early, "Вчерашняя", yesterday, None, 4)

    await run_tick(db, early, config, sender)
    assert sender.messages == []
    early_row = await db.get_delivery_by_key(42, "digest:2026-11-16")
    assert early_row is not None
    assert early_row.status == "pending"
    assert "04:00:00" in dump_dt(early_row.scheduled_for)

    morning = clock_at(2026, 11, 16, 9, 0)
    await run_tick(db, morning, config, sender)
    assert len(sender.messages) == 1
    assert sender.messages[0]["chat_id"] == 42
    text = sender.messages[0]["text"]
    assert "Утренняя сводка на 16.11.2026" in text
    assert "Открытых на сегодня: 2" in text
    assert "Созвон &lt;b&gt;" in text
    assert "<b>" not in text
    assert "Без часа" in text
    assert text.index("Задачи со временем") < text.index("Созвон")
    assert text.index("Созвон") < text.index("Просроченные")
    assert text.index("Просроченные") < text.index("Уже было")
    assert text.index("Просроченные") < text.index("Вчерашняя")
    callbacks = [data for _, data in button_rows(sender.messages[0]["markup"])]
    assert callbacks == ["l:td:0", "l:al:0"]
    sent = await db.get_delivery_by_key(42, "digest:2026-11-16")
    assert sent.status == "sent"

    await run_tick(db, morning, config, sender)
    assert len(sender.messages) == 1
    await db.close()

    reopened = await open_db(path / "morning.sqlite3")
    await run_tick(reopened, morning, config, sender)
    assert len(sender.messages) == 1
    settings = await reopened.ensure_settings(42, morning.now_utc())
    settings.digest_enabled = False
    await reopened.save_settings(settings, morning.now_utc())
    settings.digest_enabled = True
    settings.digest_time = time(10, 0)
    await reopened.save_settings(settings, morning.now_utc())
    await run_tick(reopened, clock_at(2026, 11, 16, 10, 0), config, sender)
    assert len(sender.messages) == 1
    digest_rows = [item for item in await reopened.list_deliveries(42) if item.kind == "digest"]
    assert [item.due_key for item in digest_rows] == ["digest:2026-11-16"]
    stored = {task.title: task.task_date for task in await reopened.list_tasks(42, "open")}
    assert stored["Вчерашняя"] == yesterday
    assert stored["Уже было"] == day
    await reopened.close()

    utc_morning = await _quiet_digest(path / "utc.sqlite3", clock_at(2026, 11, 16, 14, 0))
    assert utc_morning.status == "skipped"
    assert utc_morning.cancel_reason == "late_digest"


async def _missed_digest_window(path):
    within = await _send_once(path / "within.sqlite3", clock_at(2026, 11, 16, 10, 30))
    assert len(within.messages) == 1
    assert "16.11.2026" in within.messages[0]["text"]
    keys = await _digest_keys(path / "within.sqlite3")
    assert keys == ["digest:2026-11-16"]

    boundary = await _send_once(path / "boundary.sqlite3", clock_at(2026, 11, 16, 11, 0))
    assert len(boundary.messages) == 1

    late = await _quiet_digest(path / "late.sqlite3", clock_at(2026, 11, 16, 12, 1))
    assert late.status == "skipped"
    assert late.cancel_reason == "late_digest"
    minute_late = await _quiet_digest(path / "minute.sqlite3", clock_at(2026, 11, 16, 11, 1))
    assert minute_late.status == "skipped"


async def _empty_digest_is_recorded_without_message(path):
    db, config, sender = await _world(path / "empty.sqlite3")
    clock = clock_at(2026, 11, 16, 9, 0)
    await run_tick(db, clock, config, sender)
    assert sender.messages == []
    row = await db.get_delivery_by_key(42, "digest:2026-11-16")
    assert row.status == "skipped"
    assert row.cancel_reason == "empty_digest"
    await _add(db, clock, "Появилась позже", clock.today(), None, 1)
    await run_tick(db, clock, config, sender)
    assert sender.messages == []
    assert (await db.get_delivery_by_key(42, "digest:2026-11-16")).status == "skipped"
    await db.close()


async def _digest_compacts_a_long_list(path):
    db, config, sender = await _world(path / "long.sqlite3")
    clock = clock_at(2026, 11, 16, 9, 0)
    for number in range(1, 31):
        await _add(
            db,
            clock,
            f"Задача {number:02d} " + ("слово " * 30),
            clock.today(),
            None,
            number,
        )
    await run_tick(db, clock, config, sender)
    text = sender.messages[0]["text"]
    assert len(text) <= 3900
    assert "Без времени: 30" in text
    assert "и ещё" in text
    assert "Задача 01" in text
    await db.close()


async def _local_midnight_digest_keeps_tashkent_date(path):
    db, config, sender = await _world(path / "midnight.sqlite3")
    clock = clock_at(2026, 11, 17, 0, 30)
    settings = await db.ensure_settings(42, clock.now_utc())
    settings.digest_time = time(0, 30)
    await db.save_settings(settings, clock.now_utc())
    await _add(db, clock, "Ночная", clock.today(), None, 1)
    await run_tick(db, clock, config, sender)
    assert "17.11.2026" in sender.messages[0]["text"]
    row = await db.get_delivery_by_key(42, "digest:2026-11-17")
    assert row.status == "sent"
    assert dump_dt(row.scheduled_for).startswith("2026-11-16T19:30:00")
    await db.close()


async def _regular_reminder_and_near_due(path):
    db, config, sender = await _world(path / "reminder.sqlite3")
    clock = clock_at(2026, 11, 16, 14, 30)
    day = clock.today()
    task = await _add(db, clock, "<b>кот</b>", day, to_utc(day, time(15, 0), TZ), 1)
    await run_tick(db, clock, config, sender)
    assert len(sender.messages) == 1
    text = sender.messages[0]["text"]
    assert text.startswith("Напоминание")
    assert "Время задачи уже прошло." not in text
    assert "&lt;b&gt;кот&lt;/b&gt;" in text
    assert "<b>кот</b>" not in text
    assert "16.11.2026, 15:00" in text
    assert sender.messages[0]["chat_id"] == 42
    buttons = button_rows(sender.messages[0]["markup"])
    assert [label for label, _ in buttons] == [
        "✅ Готово",
        "⏰ Через 15 минут",
        "⏰ Через час",
        "📅 Перенести",
    ]
    assert buttons[0][1] == f"d:{task.id}:al:0"
    assert buttons[3][1] == f"m:{task.id}:al:0"
    await run_tick(db, clock, config, sender)
    assert len(sender.messages) == 1
    await db.close()

    near_db, near_config, near_sender = await _world(path / "near.sqlite3")
    near_clock = clock_at(2026, 11, 16, 14, 50)
    near_day = near_clock.today()
    await _add(near_db, near_clock, "Скоро", near_day, to_utc(near_day, time(15, 0), TZ), 1)
    await run_tick(near_db, near_clock, near_config, near_sender)
    await run_tick(near_db, near_clock, near_config, near_sender)
    assert len(near_sender.messages) == 1
    assert near_sender.messages[0]["text"].startswith("Напоминание")
    await near_db.close()


async def _missed_reminder_grace(path):
    within, within_sender = await _planned_reminder(path / "within.sqlite3")
    await run_tick(within[0], clock_at(2026, 11, 16, 15, 15), within[1], within_sender)
    assert len(within_sender.messages) == 1
    assert "Время задачи уже прошло." in within_sender.messages[0]["text"]
    await within[0].close()

    late, late_sender = await _planned_reminder(path / "late.sqlite3")
    await run_tick(late[0], clock_at(2026, 11, 16, 15, 16), late[1], late_sender)
    assert late_sender.messages == []
    row = [item for item in await late[0].list_deliveries(42) if item.kind == "reminder"][0]
    assert row.status == "skipped"
    assert row.cancel_reason == "missed_reminder"
    await late[0].close()


async def _planned_reminder(path):
    db, config, sender = await _world(path)
    clock = clock_at(2026, 11, 16, 12, 0)
    day = clock.today()
    await _add(db, clock, "Опоздать", day, to_utc(day, time(15, 0), TZ), 1)
    await run_tick(db, clock, config, sender)
    assert sender.messages == []
    return (db, config), sender


async def _downtime_sends_reminder_while_due_is_ahead(path):
    db, config, sender = await _world(path / "down.sqlite3")
    early = clock_at(2026, 11, 16, 12, 0)
    day = early.today()
    await _add(db, early, "После простоя", day, to_utc(day, time(15, 0), TZ), 1)
    await run_tick(db, early, config, sender)
    assert sender.messages == []
    await run_tick(db, clock_at(2026, 11, 16, 14, 40), config, sender)
    assert len(sender.messages) == 1
    assert sender.messages[0]["text"].startswith("Напоминание")
    assert "Время задачи уже прошло." not in sender.messages[0]["text"]
    await run_tick(db, clock_at(2026, 11, 16, 14, 40), config, sender)
    assert len(sender.messages) == 1
    await db.close()


async def _no_reminder_for_timeless_or_already_overdue(path):
    db, config, sender = await _world(path / "none.sqlite3")
    clock = clock_at(2026, 11, 16, 14, 0)
    day = clock.today()
    await _add(db, clock, "Без времени", day, None, 1)
    await _add(db, clock, "Уже прошла", day, to_utc(day, time(13, 0), TZ), 2)
    await run_tick(db, clock, config, sender)
    assert sender.messages == []
    reminders = [item for item in await db.list_deliveries(42) if item.kind == "reminder"]
    assert reminders == []
    await db.close()


async def _task_changes_cancel_pending_reminder(path):
    db, config, sender = await _world(path / "cancel.sqlite3")
    clock = clock_at(2026, 11, 16, 12, 0)
    day = clock.today()
    done_task = await _add(db, clock, "Закрыть", day, to_utc(day, time(16, 0), TZ), 1)
    delete_task = await _add(db, clock, "Удалить", day, to_utc(day, time(16, 30), TZ), 2)
    move_task = await _add(db, clock, "Перенести", day, to_utc(day, time(16, 0), TZ), 3)
    await run_tick(db, clock, config, sender)
    assert sender.messages == []

    await on_done(
        FakeCallback(f"d:{done_task.id}:al:0", FakeMessage()),
        db,
        clock,
        config,
        new_locks(),
    )
    await on_delete_yes(
        FakeCallback(f"y:{delete_task.id}", FakeMessage()),
        db,
        config,
        new_locks(),
    )
    await on_move_start(
        FakeCallback(f"m:{move_task.id}:al:0", FakeMessage()),
        FakeState(),
        db,
        clock,
        config,
        new_locks(),
    )
    await db.set_schedule(
        42,
        move_task.id,
        day,
        to_utc(day, time(18, 0), TZ),
        clock.now_utc(),
    )
    fire = clock_at(2026, 11, 16, 15, 30)
    await run_tick(db, fire, config, sender)
    assert sender.messages == []
    moved = clock_at(2026, 11, 16, 17, 30)
    await run_tick(db, moved, config, sender)
    assert len(sender.messages) == 1
    assert "Перенести" in sender.messages[0]["text"]
    assert (await db.get_task(42, move_task.id)).due_at_utc == to_utc(day, time(18, 0), TZ)
    await db.close()


async def _snooze_persists_and_ignores_repeat(path):
    db, config, sender = await _world(path / "snooze.sqlite3")
    clock = clock_at(2026, 11, 16, 14, 30)
    day = clock.today()
    task = await _add(db, clock, "Позвонить", day, to_utc(day, time(15, 0), TZ), 1)
    await run_tick(db, clock, config, sender)
    assert len(sender.messages) == 1
    due_before = (await db.get_task(42, task.id)).due_at_utc
    reminder = [item for item in await db.list_deliveries(42) if item.kind == "reminder"][0]
    locks = new_locks()
    first = FakeCallback(f"sn:{reminder.id}:15", FakeMessage())
    await on_snooze(first, db, clock, config, locks)
    second = FakeCallback(f"sn:{reminder.id}:15", FakeMessage())
    await on_snooze(second, db, clock, config, locks)
    snoozes = [item for item in await db.list_deliveries(42) if item.kind == "snooze"]
    assert len(snoozes) == 1
    assert snoozes[0].status == "pending"
    assert dump_dt(snoozes[0].scheduled_for).endswith("09:45:00+00:00")
    assert (await db.get_task(42, task.id)).due_at_utc == due_before
    assert "14:45" in first.message.answers[-1]["text"]
    assert "уже назначено" in second.message.answers[-1]["text"]
    assert "15:00" in second.message.answers[-1]["text"]
    await on_snooze(FakeCallback(f"sn:{reminder.id}:60", FakeMessage()), db, clock, config, locks)
    snoozes = [item for item in await db.list_deliveries(42) if item.kind == "snooze"]
    pending = [item for item in snoozes if item.status == "pending"]
    assert len(pending) == 1
    assert pending[0].due_key.endswith(":60")
    assert any(item.status == "cancelled" and item.due_key.endswith(":15") for item in snoozes)
    await db.close()

    restarted = await open_db(path / "snooze.sqlite3")
    await run_tick(restarted, clock_at(2026, 11, 16, 15, 30), config, sender)
    assert len(sender.messages) == 2
    assert "Отложенное напоминание" in sender.messages[-1]["text"]
    assert (await restarted.get_task(42, task.id)).due_at_utc == due_before
    await on_snooze(
        FakeCallback(f"sn:{reminder.id}:60", FakeMessage()),
        restarted,
        clock_at(2026, 11, 16, 15, 30),
        config,
        new_locks(),
    )
    assert len([item for item in await restarted.list_deliveries(42) if item.kind == "snooze"]) == 2
    sent = await restarted.get_delivery_by_key(42, pending[0].due_key)
    assert sent.status == "sent"
    await run_tick(restarted, clock_at(2026, 11, 16, 16, 0), config, sender)
    assert len(sender.messages) == 2
    await restarted.close()


async def _snooze_can_be_after_due(path):
    db, config, sender = await _world(path / "after.sqlite3")
    clock = clock_at(2026, 11, 16, 14, 30)
    day = clock.today()
    task = await _add(db, clock, "После срока", day, to_utc(day, time(15, 0), TZ), 1)
    await run_tick(db, clock, config, sender)
    reminder = [item for item in await db.list_deliveries(42) if item.kind == "reminder"][0]
    later = clock_at(2026, 11, 16, 14, 50)
    await on_snooze(
        FakeCallback(f"sn:{reminder.id}:60", FakeMessage()),
        db,
        later,
        config,
        new_locks(),
    )
    assert (await db.get_task(42, task.id)).due_at_utc == to_utc(day, time(15, 0), TZ)
    before = len(sender.messages)
    await run_tick(db, clock_at(2026, 11, 16, 15, 50), config, sender)
    assert len(sender.messages) == before + 1
    assert "Срок задачи уже прошёл" in sender.messages[-1]["text"]
    await db.close()


async def _settings_change_lead_and_toggles(path):
    db, config, sender = await _world(path / "settings.sqlite3")
    clock = clock_at(2026, 11, 16, 12, 0)
    day = clock.today()
    sent_task = await _add(db, clock, "Уже напомнили", day, to_utc(day, time(12, 20), TZ), 1)
    waiting = await _add(db, clock, "Позже", day, to_utc(day, time(18, 0), TZ), 2)
    await run_tick(db, clock_at(2026, 11, 16, 11, 50), config, sender)
    assert len(sender.messages) == 1
    await run_tick(db, clock, config, sender)
    assert len(sender.messages) == 1

    message = FakeMessage(BTN_SETTINGS)
    await on_settings(message, FakeState(), db, clock, config, new_locks())
    assert "Asia/Tashkent" in message.answers[-1]["text"]
    assert "09:00" in message.answers[-1]["text"]
    assert "за 30 минут" in message.answers[-1]["text"]

    await on_lead(
        FakeCallback("st:lead:60", FakeMessage()),
        FakeState(),
        db,
        clock,
        config,
        new_locks(),
    )
    sent_rows = [
        item
        for item in await db.list_deliveries(42, task_id=sent_task.id)
        if item.kind == "reminder"
    ]
    waiting_rows = [
        item
        for item in await db.list_deliveries(42, task_id=waiting.id)
        if item.kind == "reminder"
    ]
    assert len(sent_rows) == 1 and sent_rows[0].status == "sent"
    assert len(waiting_rows) == 1
    assert dump_dt(waiting_rows[0].scheduled_for).endswith("12:00:00+00:00")
    await run_tick(db, clock, config, sender)
    assert len(sender.messages) == 1

    state = FakeState()
    await state.set_state(SettingsDialog.enter_time)
    invalid = FakeMessage("25:99")
    await on_digest_time_text(invalid, state, db, clock, config, new_locks())
    assert (await db.get_settings(42)).digest_time == time(9, 0)
    assert await state.get_state() == SettingsDialog.enter_time.state
    await on_digest_time_text(FakeMessage("7:05"), state, db, clock, config, new_locks())
    assert (await db.get_settings(42)).digest_time == time(7, 5)
    assert await state.get_state() is None

    reminder = sent_rows[0]
    await on_snooze(
        FakeCallback(f"sn:{reminder.id}:15", FakeMessage()),
        db,
        clock,
        config,
        new_locks(),
    )
    await on_toggle_digest(
        FakeCallback("st:digest", FakeMessage()),
        FakeState(),
        db,
        clock,
        config,
        new_locks(),
    )
    assert (await db.get_settings(42)).digest_enabled is False
    await on_settings_reminders_off_and_on(db, clock, config, sender, waiting.id)
    await db.close()


async def on_settings_reminders_off_and_on(db, clock, config, sender, waiting_id):
    from taskbot.handlers.settings import on_toggle_reminders

    snooze = [item for item in await db.list_deliveries(42) if item.kind == "snooze"]
    assert len(snooze) == 1 and snooze[0].status == "pending"
    await on_toggle_reminders(
        FakeCallback("st:remind", FakeMessage()),
        FakeState(),
        db,
        clock,
        config,
        new_locks(),
    )
    assert (await db.get_settings(42)).reminders_enabled is False
    rows = await db.list_deliveries(42)
    waiting = [
        item for item in rows if item.task_id == waiting_id and item.kind == "reminder"
    ][0]
    assert waiting.status == "cancelled"
    assert waiting.cancel_reason == "reminders_off"
    assert [item for item in rows if item.kind == "snooze"][0].status == "cancelled"
    before = len(sender.messages)
    await run_tick(db, clock_at(2026, 11, 16, 17, 0), config, sender)
    assert len(sender.messages) == before
    await on_toggle_reminders(
        FakeCallback("st:remind", FakeMessage()),
        FakeState(),
        db,
        clock,
        config,
        new_locks(),
    )
    restored = [
        item
        for item in await db.list_deliveries(42)
        if item.task_id == waiting_id and item.kind == "reminder"
    ][0]
    assert restored.status == "pending"
    assert [item for item in await db.list_deliveries(42) if item.kind == "snooze"][0].status == "cancelled"


async def _temporary_error_waits_before_retry(path):
    db, config, sender = await _world(path / "temp.sqlite3")
    moment = datetime(2026, 11, 16, 14, 30, tzinfo=TZ)
    await _add(db, Clock(TZ, now=moment), "Сеть", moment.date(), to_utc(moment.date(), time(15, 0), TZ), 1)
    sender.errors.append(TelegramNetworkError(SimpleNamespace(), "temporary down"))
    await run_tick(db, Clock(TZ, now=moment), config, sender)
    row = [item for item in await db.list_deliveries(42) if item.kind == "reminder"][0]
    assert sender.calls == 1
    assert sender.messages == []
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.next_attempt_at == moment.astimezone(timezone.utc) + timedelta(seconds=30)
    await run_tick(db, Clock(TZ, now=moment), config, sender)
    assert sender.calls == 1
    await run_tick(db, Clock(TZ, now=moment + timedelta(seconds=30)), config, sender)
    assert len(sender.messages) == 1
    assert (await db.get_delivery(42, row.id)).status == "sent"
    await db.close()


async def _retry_after_uses_given_delay(path):
    db, config, sender = await _world(path / "flood.sqlite3")
    moment = datetime(2026, 11, 16, 14, 30, tzinfo=TZ)
    await _add(db, Clock(TZ, now=moment), "Лимит", moment.date(), to_utc(moment.date(), time(15, 0), TZ), 1)
    sender.errors.append(TelegramRetryAfter(SimpleNamespace(), "flood", 45))
    await run_tick(db, Clock(TZ, now=moment), config, sender)
    row = [item for item in await db.list_deliveries(42) if item.kind == "reminder"][0]
    assert row.next_attempt_at == moment.astimezone(timezone.utc) + timedelta(seconds=45)
    await run_tick(db, Clock(TZ, now=moment), config, sender)
    assert sender.calls == 1
    await run_tick(db, Clock(TZ, now=moment + timedelta(seconds=45)), config, sender)
    assert len(sender.messages) == 1
    assert (await db.get_delivery(42, row.id)).status == "sent"
    await db.close()


async def _forbidden_stops_without_token(path):
    db, config, sender = await _world(path / "block.sqlite3")
    moment = datetime(2026, 11, 16, 14, 30, tzinfo=TZ)
    await _add(db, Clock(TZ, now=moment), "Блок", moment.date(), to_utc(moment.date(), time(15, 0), TZ), 1)
    sender.errors.append(
        TelegramForbiddenError(
            SimpleNamespace(),
            f"Forbidden: bot was blocked by the user {config.bot_token}",
        )
    )
    clock = Clock(TZ, now=moment)
    await run_tick(db, clock, config, sender)
    await run_tick(db, Clock(TZ, now=moment + timedelta(minutes=5)), config, sender)
    row = [item for item in await db.list_deliveries(42) if item.kind == "reminder"][0]
    assert sender.calls == 1
    assert sender.messages == []
    assert row.status == "blocked"
    assert row.cancel_reason == "forbidden"
    assert config.bot_token not in (row.last_error or "")
    assert "TelegramForbiddenError" in (row.last_error or "")
    await db.close()


async def _repeated_failures_stop(path):
    db, config, sender = await _world(path / "retry.sqlite3")
    moment = datetime(2026, 11, 16, 14, 30, tzinfo=TZ)
    await _add(db, Clock(TZ, now=moment), "Снова", moment.date(), to_utc(moment.date(), time(15, 0), TZ), 1)
    for _ in range(5):
        sender.errors.append(TelegramNetworkError(SimpleNamespace(), "temporary down"))
        await run_tick(db, Clock(TZ, now=moment), config, sender)
        row = [item for item in await db.list_deliveries(42) if item.kind == "reminder"][0]
        if row.next_attempt_at is not None:
            moment = row.next_attempt_at.astimezone(TZ) + timedelta(seconds=1)
    row = [item for item in await db.list_deliveries(42) if item.kind == "reminder"][0]
    assert row.status == "skipped"
    assert row.cancel_reason == "retries_exhausted"
    assert sender.calls == 5
    await run_tick(db, Clock(TZ, now=moment), config, sender)
    assert sender.calls == 5
    await db.close()


async def _stuck_processing_is_recovered(path):
    db, config, sender = await _world(path / "stuck.sqlite3")
    clock = clock_at(2026, 11, 16, 14, 30)
    day = clock.today()
    await _add(db, clock, "Зависло", day, to_utc(day, time(15, 0), TZ), 1)
    from taskbot.schedule import sync_plan

    await sync_plan(db, clock, config)
    delivery = [item for item in await db.list_deliveries(42) if item.kind == "reminder"][0]
    stale = dump_dt(clock.now_utc() - timedelta(minutes=10))
    await db._require().execute(
        """
        UPDATE deliveries
        SET status = 'processing', locked_at = ?, scheduled_for = ?
        WHERE id = ?
        """,
        (stale, dump_dt(clock.now_utc() - timedelta(minutes=1)), delivery.id),
    )
    await db._require().commit()
    await run_tick(db, clock, config, sender)
    assert len(sender.messages) == 1
    assert (await db.get_delivery(42, delivery.id)).status == "sent"

    fresh = await _world(path / "fresh.sqlite3")
    fresh_db, fresh_config, fresh_sender = fresh
    fresh_clock = clock_at(2026, 11, 16, 14, 30)
    await _add(
        fresh_db,
        fresh_clock,
        "Свежая",
        fresh_clock.today(),
        to_utc(fresh_clock.today(), time(15, 0), TZ),
        1,
    )
    await sync_plan(fresh_db, fresh_clock, fresh_config)
    current = [item for item in await fresh_db.list_deliveries(42) if item.kind == "reminder"][0]
    await fresh_db._require().execute(
        "UPDATE deliveries SET status = 'processing', locked_at = ? WHERE id = ?",
        (dump_dt(fresh_clock.now_utc()), current.id),
    )
    await fresh_db._require().commit()
    await run_tick(fresh_db, fresh_clock, fresh_config, fresh_sender)
    assert fresh_sender.messages == []
    assert (await fresh_db.get_delivery(42, current.id)).status == "processing"
    await db.close()
    await fresh_db.close()


async def _worker_stops_with_the_bot():
    stop = asyncio.Event()
    calls = []

    async def tick():
        calls.append(1)
        stop.set()

    await notification_worker(stop, tick, poll_seconds=30)
    assert calls == [1]

    started = asyncio.Event()

    async def hanging():
        started.set()
        await asyncio.sleep(30)

    async def polling():
        await started.wait()

    await asyncio.wait_for(
        poll_with_worker(polling, hanging, poll_seconds=30),
        timeout=2,
    )


async def _world(path):
    database = await open_db(path)
    return database, make_config(path), FakeSender()


async def _add(db, clock, title, day, due, message_id):
    task, created = await db.add_task(
        owner_id=42,
        title=title,
        source_text=title,
        task_date=day,
        due_at_utc=due,
        now=clock.now_utc(),
        source_chat_id=42,
        source_message_id=message_id,
        source_line=1,
    )
    assert created
    return task


async def _send_once(path, clock):
    db, config, sender = await _world(path)
    await _add(db, clock, "Есть задача", clock.today(), None, 1)
    await run_tick(db, clock, config, sender)
    await db.close()
    return sender


async def _quiet_digest(path, clock):
    db, config, sender = await _world(path)
    await _add(db, clock, "Есть задача", clock.today(), None, 1)
    await run_tick(db, clock, config, sender)
    assert sender.messages == []
    row = await db.get_delivery_by_key(42, f"digest:{clock.today().isoformat()}")
    await db.close()
    return row


async def _digest_keys(path):
    database = Database(path)
    await database.connect()
    keys = [item.due_key for item in await database.list_deliveries(42) if item.kind == "digest"]
    await database.close()
    return keys
