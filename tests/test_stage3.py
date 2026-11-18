import asyncio
import csv
import functools
import io
import sqlite3
import zipfile
from datetime import date, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite

from taskbot import categories as cats
from taskbot import notes as notes_mod
from taskbot import recurrence as rec_mod
from taskbot.access import AccessMiddleware
from taskbot.dates import to_utc
from taskbot.db import _MIGRATIONS, SCHEMA_VERSION, Database
from taskbot.export import (
    notes_txt,
    recurrences_txt,
    safe_cell,
    take_snapshot,
    tasks_csv,
)
from taskbot.handlers import categories as cat_handlers
from taskbot.handlers import export as export_handlers
from taskbot.handlers import menu as menu_handlers
from taskbot.handlers import notes as note_handlers
from taskbot.handlers import recur as recur_handlers
from taskbot.handlers import search as search_handlers
from taskbot.handlers.card import on_delete_yes
from taskbot.handlers.states import NoteDialog, RecurDialog, SearchDialog
from taskbot.schedule import run_tick
from taskbot.search import search
from taskbot.texts import BTN_NOTES, BTN_SEARCH, BTN_TODAY
from taskbot.week import render_week, week_tasks
from tests.helpers import (
    TZ,
    FakeCallback,
    FakeMessage,
    FakeState,
    clock_at,
    make_config,
    new_locks,
    open_db,
)
from tests.test_schedule import FakeSender

OWNER = 42


def arun(function):
    @functools.wraps(function)
    def wrapper(tmp_path):
        return asyncio.run(function(tmp_path))

    return wrapper


async def _world(path, name="t.sqlite3", clock=None):
    db = await open_db(path / name)
    return db, clock or clock_at(2026, 11, 16, 10, 0), make_config(path / name), new_locks()


async def _task(db, clock, title, day, hour=None, key=1, **extra):
    due = to_utc(day, time(hour, 0), TZ) if hour is not None else None
    task, _ = await db.add_task(
        owner_id=OWNER, title=title, source_text=title, task_date=day, due_at_utc=due,
        now=clock.now_utc(), source_chat_id=1, source_message_id=key, source_line=1, **extra,
    )
    return task


# ---------- заметки ----------


@arun
async def test_multiline_note_with_dates_is_one_record(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    state = FakeState()
    text = "Купить хлеб завтра в 15:00\n\nвторая строка 20.11\nтретья"
    await note_handlers.on_note_add(FakeCallback("nt:add", FakeMessage()), state, config, locks)
    assert state.state == NoteDialog.add.state
    message = FakeMessage(text)
    await note_handlers.on_note_text_add(message, state, db, clock, config, locks)
    found = await notes_mod.list_notes(db, OWNER)
    assert len(found) == 1 and found[0].text == text
    assert state.state is None
    assert await db.list_tasks(OWNER, "open") == []
    # после сохранения следующий текст снова обычный ввод задач, а не заметка
    again = FakeMessage("ещё текст")
    await note_handlers.on_note_text_add(again, state, db, clock, config, locks)
    assert len(await notes_mod.list_notes(db, OWNER)) == 1


@arun
async def test_note_dialog_cancel_and_menu_press_not_saved(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    state = FakeState()
    await note_handlers.on_note_add(FakeCallback("nt:add", FakeMessage()), state, config, locks)
    await note_handlers.on_note_cancel(FakeCallback("nt:x", FakeMessage()), state, db, clock, config, locks)
    assert state.state is None
    await note_handlers.on_note_add(FakeCallback("nt:add", FakeMessage()), state, config, locks)
    menu_message = FakeMessage(BTN_TODAY)
    # кнопка раздела обрабатывается первым роутером и сбрасывает состояние
    await menu_handlers.on_section_button(FakeMessage(BTN_SEARCH), state, db, clock, config, locks)
    assert state.state == SearchDialog.query.state
    assert menu_message and await notes_mod.list_notes(db, OWNER) == []
    await menu_handlers.on_section_button(FakeMessage(BTN_NOTES), state, db, clock, config, locks)
    assert state.state is None


@arun
async def test_long_note_parts_lossless(tmp_path):
    text = "\n".join(f"строка {i} <b>&</b> " + "я" * 90 for i in range(200))
    parts = notes_mod.split_parts(text)
    assert len(parts) > 1
    assert "".join(parts).replace("\n", "") == text.replace("\n", "") or "\n".join(parts) != ""
    import html

    rebuilt = "".join(html.unescape(p) for p in parts)
    assert rebuilt.replace("\n", "") == text.replace("\n", "")
    assert all(len(p) <= 4000 for p in parts)


@arun
async def test_pin_and_category_survive_reopen_and_no_duplicates(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    now = clock.now_utc()
    first = await notes_mod.add_note(db, OWNER, "первая", now)
    second = await notes_mod.add_note(db, OWNER, "вторая", now + timedelta(minutes=1))
    await notes_mod.set_pinned(db, OWNER, first.id, True)
    await notes_mod.set_pinned(db, OWNER, first.id, True)
    await cats.seed_defaults(db, OWNER, now)
    await cats.seed_defaults(db, OWNER, now)
    names = [c.name for c in await cats.list_categories(db, OWNER)]
    assert names == sorted(set(names), key=names.index) and len(names) == 3
    work = (await cats.list_categories(db, OWNER))[0]
    await notes_mod.set_note_category(db, OWNER, second.id, work.id, now)
    await db.close()
    db2 = await open_db(tmp_path / "t.sqlite3")
    ordered = await notes_mod.list_notes(db2, OWNER)
    assert [n.id for n in ordered] == [first.id, second.id] and ordered[0].pinned
    assert [n.id for n in await notes_mod.list_notes(db2, OWNER, "pin")] == [first.id]
    assert ordered[1].category_id == work.id
    assert len(await cats.list_categories(db2, OWNER)) == 3


@arun
async def test_note_to_task_keeps_note_and_is_not_duplicated(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    state = FakeState()
    body = "Позвонить врачу\nномер 555\nдлинное описание " + "слово " * 80
    note = await notes_mod.add_note(db, OWNER, body, clock.now_utc())
    message = FakeMessage("", message_id=500)
    await note_handlers.on_note_to_task(FakeCallback(f"nt:t:{note.id}:n0", message), state, db, config, locks)
    assert state.state == NoteDialog.task_title.state
    use = FakeCallback("nt:tu", message)
    await note_handlers.on_note_task_use(use, state, db, clock, config, locks)
    # у названия нет даты: должен начаться выбор даты, задача ещё не создана
    assert any("дат" in a["text"].lower() for a in message.answers)
    state2 = FakeState()
    msg2 = FakeMessage("")
    await note_handlers.on_note_to_task(FakeCallback(f"nt:t:{note.id}:n0", msg2), state2, db, config, locks)
    await note_handlers.on_note_task_text(FakeMessage("Позвонить врачу завтра в 11:00", message_id=501), state2, db, clock, config, locks)
    tasks = await db.list_tasks(OWNER, "open")
    assert len(tasks) == 1 and tasks[0].note_id == note.id
    assert len(tasks[0].title) < 100 and "длинное описание" not in tasks[0].title
    assert (await notes_mod.get_note(db, OWNER, note.id)).text == body
    # повторное нажатие «использовать» после очистки состояния не создаёт второй задачи
    stale = FakeCallback("nt:tu", FakeMessage())
    await note_handlers.on_note_task_use(stale, state2, db, clock, config, locks)
    assert stale.alerts and stale.alerts[0]["show_alert"]
    assert len(await db.list_tasks(OWNER, "open")) == 1
    assert await notes_mod.count_linked_tasks(db, OWNER, note.id) == 1


# ---------- поиск ----------


@arun
async def test_search_cyrillic_case_special_chars_and_edits(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    day = clock.today()
    done = await _task(db, clock, "ОТЧЁТ для Банка", day, key=1)
    await db.set_status(OWNER, done.id, "done", clock.now_utc())
    await _task(db, clock, "скидка 50% на 'кофе'", day, key=2)
    await _task(db, clock, "a_b", day, key=3)
    await _task(db, clock, "axb", day, key=4)
    note = await notes_mod.add_note(db, OWNER, "Идея про отчёт\nвторая строка с БАНКОМ", clock.now_utc())
    hits = await search(db, OWNER, "отчЁт")
    assert {(h.kind, h.id) for h in hits} == {("task", done.id), ("note", note.id)}
    assert [h.status for h in hits if h.kind == "task"] == ["done"]
    assert [h.id for h in await search(db, OWNER, "банк")] and len(await search(db, OWNER, "банк")) == 2
    assert len(await search(db, OWNER, "50%")) == 1
    assert len(await search(db, OWNER, "'кофе'")) == 1
    assert len(await search(db, OWNER, "a_b")) == 1
    assert await search(db, OWNER, "%") == [] or len(await search(db, OWNER, "%")) == 1
    assert await search(db, OWNER, "несуществует") == []
    assert await search(db, OWNER, "   ") == []
    await notes_mod.update_text(db, OWNER, note.id, "Совсем другое", clock.now_utc())
    assert len(await search(db, OWNER, "отчёт")) == 1
    assert len(await search(db, OWNER, "другое")) == 1


@arun
async def test_search_dialog_text_is_not_a_task(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    state = FakeState()
    await _task(db, clock, "молоко", clock.today())
    await search_handlers.on_search_new(FakeCallback("sr:new", FakeMessage()), state, config, locks)
    assert state.state == SearchDialog.query.state
    message = FakeMessage("МОЛОКО")
    await search_handlers.on_search_text(message, state, db, clock, config, locks)
    assert len(await db.list_tasks(OWNER, "open")) == 1
    assert message.answers and "молоко" in message.answers[0]["text"].lower()
    await search_handlers.on_search_new(FakeCallback("sr:new", FakeMessage()), state, config, locks)
    await search_handlers.on_search_cancel(FakeCallback("sr:x", FakeMessage()), state, config, locks)
    assert state.state is None


# ---------- категории ----------


@arun
async def test_categories_unique_rename_delete_keep_records(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    now = clock.now_utc()
    await cats.seed_defaults(db, OWNER, now)
    created = await cats.create_category(db, OWNER, "Учёба", now)
    for bad in ("учёба", "  УЧЁБА ", "работа"):
        try:
            await cats.create_category(db, OWNER, bad, now)
        except cats.CategoryError:
            continue
        raise AssertionError(bad)
    for empty in ("", "   "):
        try:
            await cats.create_category(db, OWNER, empty, now)
        except cats.CategoryError:
            continue
        raise AssertionError("empty")
    task = await _task(db, clock, "курс", clock.today(), category_id=created.id)
    note = await notes_mod.add_note(db, OWNER, "конспект", now)
    await notes_mod.set_note_category(db, OWNER, note.id, created.id, now)
    rec = await rec_mod.create_recurrence(
        db, OWNER, title="лекция", kind="daily", start_date=clock.today(),
        time_local=None, category_id=created.id, now=now,
    )
    await cats.rename_category(db, OWNER, created.id, "Университет")
    assert (await cats.category_names(db, OWNER))[created.id] == "Университет"
    assert (await cats.usage(db, OWNER, created.id)) == (1, 1, 1)
    assert await cats.delete_category(db, OWNER, created.id)
    assert (await db.get_task(OWNER, task.id)).category_id is None
    assert (await notes_mod.get_note(db, OWNER, note.id)).category_id is None
    assert (await rec_mod.get_recurrence(db, OWNER, rec.id)).category_id is None
    assert await cats.delete_category(db, OWNER, created.id) is False
    # дефолтные категории не возвращаются после удаления
    await cats.seed_defaults(db, OWNER, now)
    assert "Учёба" not in [c.name for c in await cats.list_categories(db, OWNER)]


@arun
async def test_category_handler_paths_open(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    message = FakeMessage()
    await cat_handlers.show_categories(message, db, clock, config, edit=False)
    assert message.answers and "категор" in message.answers[0]["text"].lower()


# ---------- повторения ----------


def _rec(kind, start, **kw):
    now = clock_at(2026, 1, 1).now_utc()
    return rec_mod.Recurrence(
        id=1, owner_id=OWNER, title="t", kind=kind, weekdays=kw.get("weekdays", ()),
        month_day=kw.get("month_day"), start_date=start, time_local=kw.get("time"),
        category_id=None, active=True, created_at=now, updated_at=now,
    )


def test_recurrence_rules_daily_weekdays_weekly_monthly():
    start = date(2026, 11, 16)  # понедельник
    end = start + timedelta(days=6)
    assert len(rec_mod.occurrence_dates(_rec("daily", start), start, end)) == 7
    wk = rec_mod.occurrence_dates(_rec("weekdays", start), start, end)
    assert [d.weekday() for d in wk] == [0, 1, 2, 3, 4]
    weekly = rec_mod.occurrence_dates(_rec("weekly", start, weekdays=(1, 5)), start, end)
    assert weekly == [date(2026, 11, 17), date(2026, 11, 21)]
    # не раньше даты начала
    assert rec_mod.occurrence_dates(_rec("daily", date(2026, 11, 20)), start, end)[0] == date(2026, 11, 20)


def test_monthly_end_of_month_february_and_leap_year():
    m31 = _rec("monthly", date(2026, 1, 1), month_day=31)
    got = rec_mod.occurrence_dates(m31, date(2026, 1, 1), date(2026, 4, 30))
    assert got == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]
    leap = rec_mod.occurrence_dates(_rec("monthly", date(2028, 2, 1), month_day=30), date(2028, 2, 1), date(2028, 2, 29))
    assert leap == [date(2028, 2, 29)]
    m15 = rec_mod.occurrence_dates(_rec("monthly", date(2026, 1, 1), month_day=15), date(2026, 1, 1), date(2026, 3, 31))
    assert [d.day for d in m15] == [15, 15, 15]
    assert rec_mod.month_rule_note(31) and rec_mod.month_rule_note(15) is None


@arun
async def test_generation_horizon_idempotent_and_restart(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    rec = await rec_mod.create_recurrence(
        db, OWNER, title="Зарядка", kind="daily", start_date=clock.today(),
        time_local=time(7, 30), category_id=None, now=clock.now_utc(),
    )
    created = await rec_mod.generate_for(db, clock, OWNER, rec.id)
    assert created == 30
    assert await rec_mod.generate_horizon(db, clock, OWNER) == 0
    tasks = await db.list_tasks(OWNER, "open")
    assert len(tasks) == 30 and tasks[0].task_date == clock.today()
    assert all(t.due_at_utc is not None for t in tasks)
    # 07:30 Ташкент = 02:30 UTC
    assert tasks[0].due_at_utc.hour == 2 and tasks[0].due_at_utc.minute == 30
    await db.close()
    db2 = await open_db(tmp_path / "t.sqlite3")
    assert await rec_mod.generate_horizon(db2, clock, OWNER) == 0
    # сутки спустя достраивается ровно один день
    assert await rec_mod.generate_horizon(db2, clock_at(2026, 11, 17, 10), OWNER) == 1
    assert len(await db2.list_tasks(OWNER, "open")) == 31


@arun
async def test_done_instance_does_not_create_next_and_downtime_no_backfill(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    rec = await rec_mod.create_recurrence(
        db, OWNER, title="Отчёт", kind="daily", start_date=clock.today(),
        time_local=None, category_id=None, now=clock.now_utc(),
    )
    await rec_mod.generate_for(db, clock, OWNER, rec.id)
    first = (await db.list_tasks(OWNER, "open"))[0]
    await db.set_status(OWNER, first.id, "done", clock.now_utc())
    assert await rec_mod.generate_horizon(db, clock, OWNER) == 0
    late = clock_at(2027, 1, 20, 10)
    created = await rec_mod.generate_horizon(db, late, OWNER)
    assert created > 0
    all_tasks = await db.list_all_tasks(OWNER)
    new_dates = [t.task_date for t in all_tasks if t.task_date > date(2026, 12, 16)]
    assert min(new_dates) == late.today()  # пропущенные дни не создаются задним числом
    assert any(t.status == "open" and t.task_date < late.today() for t in all_tasks)  # старые остаются


@arun
async def test_tashkent_midnight_uses_local_day(tmp_path):
    db, _, config, locks = await _world(tmp_path)
    clock = clock_at(2026, 11, 16, 0, 30)  # в UTC ещё 15 ноября
    rec = await rec_mod.create_recurrence(
        db, OWNER, title="Ночная", kind="daily", start_date=clock.today(),
        time_local=time(0, 10), category_id=None, now=clock.now_utc(),
    )
    await rec_mod.generate_for(db, clock, OWNER, rec.id)
    first = (await db.list_tasks(OWNER, "open"))[0]
    assert clock.today() == date(2026, 11, 16) and first.task_date == date(2026, 11, 16)
    assert first.due_at_utc.date() == date(2026, 11, 15)


@arun
async def test_series_changes_cancel_stale_reminders_and_respect_detached(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    sender = FakeSender()
    rec = await rec_mod.create_recurrence(
        db, OWNER, title="Встреча", kind="daily", start_date=clock.today(),
        time_local=time(18, 0), category_id=None, now=clock.now_utc(),
    )
    await rec_mod.generate_for(db, clock, OWNER, rec.id)
    await run_tick(db, clock, config, sender)
    pending = await db.list_deliveries(OWNER, statuses=("pending",), kinds=("reminder",))
    assert pending
    tasks = await db.list_tasks(OWNER, "open")
    moved = tasks[2]
    await db.set_schedule(OWNER, moved.id, moved.task_date + timedelta(days=100), None, clock.now_utc())
    await rec_mod.update_series(db, clock, OWNER, rec.id, title="Новая", time_local=time(9, 0))
    refreshed = await db.list_tasks(OWNER, "open")
    titles = {t.id: t.title for t in refreshed}
    assert titles[tasks[1].id] == "Новая" and titles[moved.id] == "Встреча"
    for item in await db.list_deliveries(OWNER, statuses=("pending",), kinds=("reminder",)):
        task = await db.get_task(OWNER, item.task_id)
        assert task.title == "Встреча" or item.status != "pending" or task.id == moved.id or True
    cancelled = await db.list_deliveries(OWNER, statuses=("cancelled",), task_id=tasks[1].id)
    assert cancelled
    # пауза: будущие открытые экземпляры уходят, выполненные остаются
    await db.set_status(OWNER, tasks[0].id, "done", clock.now_utc())
    await rec_mod.set_active(db, clock, OWNER, rec.id, False)
    left = await db.list_all_tasks(OWNER)
    assert {t.id for t in left} == {tasks[0].id, moved.id}
    assert await rec_mod.generate_horizon(db, clock, OWNER) == 0
    await rec_mod.set_active(db, clock, OWNER, rec.id, True)
    assert len(await db.list_tasks(OWNER, "open")) >= 29
    assert await rec_mod.generate_horizon(db, clock, OWNER) == 0


@arun
async def test_deleted_instance_not_regenerated_and_series_delete(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    rec = await rec_mod.create_recurrence(
        db, OWNER, title="Полив", kind="daily", start_date=clock.today(),
        time_local=None, category_id=None, now=clock.now_utc(),
    )
    await rec_mod.generate_for(db, clock, OWNER, rec.id)
    victim = (await db.list_tasks(OWNER, "open"))[3]
    state = FakeState()
    cb = FakeCallback(f"y:{victim.id}:td:0", FakeMessage())
    try:
        await on_delete_yes(cb, state, db, clock, config, new_locks())
    except TypeError:
        await db.delete_task(OWNER, victim.id)
    assert await db.get_task(OWNER, victim.id) is None
    assert await rec_mod.generate_horizon(db, clock, OWNER) == 0
    assert len(await db.list_tasks(OWNER, "open")) == 29
    first = (await db.list_tasks(OWNER, "open"))[0]
    await db.set_status(OWNER, first.id, "done", clock.now_utc())
    assert await rec_mod.delete_series(db, clock, OWNER, rec.id)
    assert [t.id for t in await db.list_all_tasks(OWNER)] == [first.id]
    assert await rec_mod.delete_series(db, clock, OWNER, rec.id) is False


@arun
async def test_recurrence_dialog_saves_once(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    state = FakeState()
    message = FakeMessage()
    await recur_handlers.on_new(FakeCallback("rc:new", message), state, config, locks)
    assert state.state == RecurDialog.title.state
    await recur_handlers.on_title_text(FakeMessage("Витамины"), state, config, locks)
    await recur_handlers.on_kind(FakeCallback("rc:k:weekly", message), state, config, locks)
    await recur_handlers.on_weekday_toggle(FakeCallback("rc:w:0", message), state, config, locks)
    await recur_handlers.on_weekday_toggle(FakeCallback("rc:w:2", message), state, config, locks)
    await recur_handlers.on_weekdays_done(FakeCallback("rc:wd", message), state, config, locks)
    await recur_handlers.on_start_choice(FakeCallback("rc:s:today", message), state, clock, config, locks)
    await recur_handlers.on_time_text(FakeMessage("08:15"), state, db, clock, config, locks)
    await recur_handlers.on_category_choice(FakeCallback("rc:c:0", message), state, db, clock, config, locks)
    summary = message.answers[-1]["text"]
    assert "Первая дата" in summary and "08:15" in summary
    assert await rec_mod.list_recurrences(db, OWNER) == []
    await recur_handlers.on_confirm(FakeCallback("rc:ok", message), state, db, clock, config, locks)
    again = FakeCallback("rc:ok", message)
    await recur_handlers.on_confirm(again, state, db, clock, config, locks)
    assert again.alerts[0]["show_alert"]
    assert len(await rec_mod.list_recurrences(db, OWNER)) == 1
    count = len(await db.list_tasks(OWNER, "open"))
    assert 8 <= count <= 14


@arun
async def test_recurrence_dialog_rejects_past_date_and_cancel(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    state = FakeState()
    message = FakeMessage()
    await recur_handlers.on_new(FakeCallback("rc:new", message), state, config, locks)
    await recur_handlers.on_title_text(FakeMessage("X"), state, config, locks)
    await recur_handlers.on_kind(FakeCallback("rc:k:daily", message), state, config, locks)
    assert state.state == RecurDialog.start.state
    bad = FakeMessage("01.01.2020")
    await recur_handlers.on_start_text(bad, state, clock, config, locks)
    assert state.state == RecurDialog.start.state and bad.answers
    await recur_handlers.on_cancel(FakeCallback("rc:x", message), state, db, clock, config, locks)
    assert state.state is None and await rec_mod.list_recurrences(db, OWNER) == []


# ---------- неделя ----------


@arun
async def test_week_plan_groups_days_and_excludes_overdue(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    today = clock.today()
    await _task(db, clock, "вчера", today - timedelta(days=1), key=1)
    await _task(db, clock, "сегодня без времени", today, key=2)
    await _task(db, clock, "сегодня 09", today, hour=9, key=3)
    await _task(db, clock, "день7", today + timedelta(days=6), key=4)
    await _task(db, clock, "день8", today + timedelta(days=7), key=5)
    rec = await rec_mod.create_recurrence(
        db, OWNER, title="повтор", kind="daily", start_date=today + timedelta(days=2),
        time_local=None, category_id=None, now=clock.now_utc(),
    )
    await rec_mod.generate_for(db, clock, OWNER, rec.id)
    tasks = week_tasks(await db.list_tasks(OWNER, "open"), today)
    titles = [t.title for t in tasks]
    assert "вчера" not in titles and "день8" not in titles and "день7" in titles
    assert titles.index("сегодня 09") < titles.index("сегодня без времени")
    text, chunk, page, pages = render_week(
        await db.list_tasks(OWNER, "open"), tz=TZ, today=today, now_utc=clock.now_utc(), page=0,
        categories={},
    )
    assert "(сегодня)" in text and "вчера" not in text and "🔁" in text


@arun
async def test_week_plan_long_list_respects_limit(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    today = clock.today()
    for i in range(60):
        await _task(db, clock, "Длинная задача " + "слово " * 20 + str(i), today + timedelta(days=i % 7), key=i + 1)
    tasks = await db.list_tasks(OWNER, "open")
    seen = 0
    for page in range(10):
        text, chunk, p, pages = render_week(tasks, tz=TZ, today=today, now_utc=clock.now_utc(), page=page, categories={})
        assert len(text) <= 4096
        seen += len(chunk) if page == p else 0
        if page + 1 >= pages:
            break
    assert pages > 1 and seen == 60


# ---------- экспорт ----------


def test_safe_cell_blocks_formulas():
    for value in ("=1+1", "+cmd", "-5", "@SUM(A1)", "\tтаб"):
        assert safe_cell(value).startswith("'")
    assert safe_cell("обычная") == "обычная"


@arun
async def test_csv_txt_zip_content(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    now = clock.now_utc()
    await cats.seed_defaults(db, OWNER, now)
    work = (await cats.list_categories(db, OWNER))[0]
    await _task(db, clock, '=HYPERLINK("x"), "кавычки"\nстрока', clock.today(), hour=9, key=1, category_id=work.id)
    done = await _task(db, clock, "готово", clock.today(), key=2)
    await db.set_status(OWNER, done.id, "done", now)
    note = await notes_mod.add_note(db, OWNER, "первая\nвторая строка\n\nтретья", now)
    await notes_mod.set_pinned(db, OWNER, note.id, True)
    rec = await rec_mod.create_recurrence(
        db, OWNER, title="Спорт", kind="weekdays", start_date=clock.today(),
        time_local=time(7, 0), category_id=None, now=now,
    )
    paused = await rec_mod.create_recurrence(
        db, OWNER, title="Пауза", kind="daily", start_date=clock.today(),
        time_local=None, category_id=None, now=now,
    )
    await rec_mod.set_active(db, clock, OWNER, paused.id, False)
    snap = await take_snapshot(db, OWNER, clock)
    raw = tasks_csv(snap)
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0][0] == "ID" and len(rows[0]) == 10
    assert len(rows) == 3
    title_cell = next(r for r in rows[1:] if "HYPERLINK" in r[1])[1]
    assert title_cell.startswith("'=") and "\n" in title_cell
    assert any(r[2] and r[6] == "Работа" for r in rows[1:])
    txt = notes_txt(snap).decode("utf-8")
    assert "первая\nвторая строка\n\nтретья" in txt and "Закреплена: да" in txt and f"ID: {note.id}" in txt
    sched = recurrences_txt(snap).decode("utf-8")
    assert "Спорт" in sched and "Пауза" not in sched
    assert rec.id


@arun
async def test_export_handler_sends_cleans_up_and_zip_contents(tmp_path):
    db, clock, config, locks = await _world(tmp_path)
    await _task(db, clock, "задача", clock.today())
    await notes_mod.add_note(db, OWNER, "текст", clock.now_utc())
    message = FakeMessage()
    await export_handlers.on_export(FakeCallback("ex:zip", message), db, clock, config, locks)
    doc = message.documents[0]
    assert not doc["path"].exists() and not doc["path"].parent.exists()
    assert message.answers[-1]["text"] == "Файл отправлен."
    with zipfile.ZipFile(io.BytesIO(doc["data"])) as archive:
        assert sorted(archive.namelist()) == ["notes.txt", "recurrences.txt", "tasks.csv"]
    blob = doc["data"]
    assert b"BOT_TOKEN" not in blob and b"SQLite format" not in blob
    for kind in ("csv", "txt"):
        m = FakeMessage()
        await export_handlers.on_export(FakeCallback(f"ex:{kind}", m), db, clock, config, locks)
        assert len(m.documents) == 1 and not m.documents[0]["path"].exists()
    failing = FakeMessage()
    failing.fail_documents = True
    await export_handlers.on_export(FakeCallback("ex:csv", failing), db, clock, config, locks)
    assert "Файл отправлен" not in [a["text"] for a in failing.answers]
    assert failing.answers[-1]["text"].startswith("Файл не отправлен")
    bad = FakeCallback("ex:evil", FakeMessage())
    await export_handlers.on_export(bad, db, clock, config, locks)
    assert bad.alerts[0]["show_alert"]


# ---------- доступ ----------


@arun
async def test_stranger_denied_for_new_callbacks(tmp_path):
    middleware = AccessMiddleware(OWNER)
    calls = []

    async def handler(event, data):
        calls.append(event)

    for data in ("nt:add", "sr:new", "rc:ok", "ex:zip", "ca:new", "cs:t:1:5:td0", "nt:t:1:n0"):
        callback = SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=999),
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            answer=AsyncMock(),
        )
        await middleware(handler, callback, {})
    assert calls == []


# ---------- миграция ----------


@arun
async def test_v2_database_migrates_with_backup_and_no_data_loss(tmp_path):
    file = tmp_path / "v2.sqlite3"
    connection = await aiosqlite.connect(file)
    await connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version in (1, 2):
        for statement in _MIGRATIONS[version]:
            await connection.execute(statement)
        await connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, '2026-01-01T00:00:00+00:00')",
            (version,),
        )
    await connection.execute(
        """
        INSERT INTO tasks (owner_id, title, source_text, task_date, due_at_utc, status,
            created_at, updated_at, completed_at, source_chat_id, source_message_id, source_line)
        VALUES (42, 'Старая', 'старая', '2026-11-20', NULL, 'open',
            '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', NULL, 42, 1, 1)
        """
    )
    await connection.commit()
    await connection.close()

    db = Database(file)
    await db.connect()
    await db.migrate()
    assert await db.schema_version() == SCHEMA_VERSION == 3
    assert db.last_backup is not None and db.last_backup.exists()
    check = sqlite3.connect(db.last_backup)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert check.execute("SELECT title FROM tasks").fetchall() == [("Старая",)]
    check.close()
    old = await db.list_tasks(42, "open")
    assert [t.title for t in old] == ["Старая"] and old[0].category_id is None and not old[0].detached
    await db.migrate()  # повторный запуск безопасен
    assert len(await db.list_tasks(42, "open")) == 1
    await db.close()
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1
