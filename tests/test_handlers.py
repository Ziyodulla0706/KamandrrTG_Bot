import asyncio
from datetime import date, time

from taskbot.dates import from_utc
from taskbot.handlers.card import on_delete_yes, on_done, on_move_callback, on_move_start, on_rename_cancel, on_rename_start, on_rename_text
from taskbot.handlers.intake import handle_new_tasks, on_clarify_callback
from taskbot.handlers.states import Clarify, Rename
from taskbot.parsing import parse_exact_time
from tests.helpers import FakeCallback, FakeMessage, FakeState, button_rows, clock_at, make_config, new_locks, open_db


def test_save_show_confirmation_and_keeps_time_without_inventing_one(tmp_path):
    asyncio.run(_single_save(tmp_path))


def test_tomorrow_near_tashkent_midnight_uses_local_date(tmp_path):
    asyncio.run(_midnight(tmp_path))


def test_multiline_does_not_copy_date_and_repeat_does_not_duplicate(tmp_path):
    asyncio.run(_multiline(tmp_path))


def test_past_date_is_not_moved_to_next_year(tmp_path):
    asyncio.run(_past(tmp_path))


def test_invalid_and_ambiguous_lines(tmp_path):
    asyncio.run(_invalid_and_ambiguous(tmp_path))


def test_cancel_stops_the_dialog(tmp_path):
    asyncio.run(_cancel(tmp_path))


def test_repeated_done_and_delete(tmp_path):
    asyncio.run(_done_and_delete(tmp_path))


def test_reschedule_keeps_old_time_only_after_explicit_choice(tmp_path):
    asyncio.run(_reschedule(tmp_path))


def test_edit_cancel_really_stops_and_owner_is_config(tmp_path):
    asyncio.run(_edit_and_owner(tmp_path))


async def _single_save(path):
    db = await open_db(path / "tasks.sqlite3")
    clock = clock_at(2026, 11, 16, 12, 0)
    config = make_config(path / "tasks.sqlite3")
    state = FakeState()
    message = FakeMessage("Сегодня проверить договор", message_id=4)
    await handle_new_tasks(message, state, db, clock, config)
    tasks = await db.list_tasks(42, "open")
    assert len(tasks) == 1
    assert tasks[0].title == "Проверить договор"
    assert tasks[0].task_date == date(2026, 11, 16)
    assert tasks[0].due_at_utc is None
    text = message.answers[-1]["text"]
    assert text.startswith("Задача сохранена")
    assert "Проверить договор" in text
    assert "16.11.2026" in text
    assert "Без времени" in text
    labels = button_rows(message.answers[-1]["markup"])
    assert ("Открыть задачу", f"o:al:0:{tasks[0].id}") in labels
    assert any(label == "✏️ Изменить" for label, _ in labels)
    await db.close()


async def _midnight(path):
    db = await open_db(path / "tasks.sqlite3")
    clock = clock_at(2026, 3, 21, 0, 30)
    assert clock.now_utc().date() == date(2026, 3, 20)
    config = make_config(path / "tasks.sqlite3")
    message = FakeMessage("завтра отправить КП", message_id=3)
    await handle_new_tasks(message, FakeState(), db, clock, config)
    task = (await db.list_tasks(42, "open"))[0]
    assert task.task_date == date(2026, 3, 22)
    assert task.due_at_utc is None
    await db.close()


async def _multiline(path):
    db = await open_db(path / "tasks.sqlite3")
    clock = clock_at(2026, 11, 16, 12, 0)
    config = make_config(path / "tasks.sqlite3")
    state = FakeState()
    text = "завтра в 15:00 первое\nвторое без даты\n20.11.2026 третье"
    message = FakeMessage(text, message_id=11, user_id=999)
    await handle_new_tasks(message, state, db, clock, config)
    stored = await db.list_tasks(42, "open")
    assert len(stored) == 2
    assert {task.task_date for task in stored} == {date(2026, 11, 17), date(2026, 11, 20)}
    assert all(task.owner_id == 42 for task in stored)
    assert state.state == Clarify.choose_date.state
    question = message.answers[-1]["text"]
    assert "второе без даты" in question
    assert "Когда выполнить" in question
    callback = FakeCallback("c:today:0", message)
    await on_clarify_callback(callback, state, db, clock, config, new_locks())
    stored = await db.list_tasks(42, "open")
    assert len(stored) == 3
    pending = next(task for task in stored if task.title == "Второе без даты")
    assert pending.task_date == date(2026, 11, 16)
    assert pending.due_at_utc is None
    assert state.state is None

    again = FakeCallback("c:today:0", message)
    await on_clarify_callback(again, state, db, clock, config, new_locks())
    assert len(await db.list_tasks(42, "open")) == 3
    assert again.alerts[-1]["show_alert"] is True

    replay = FakeMessage(text, message_id=11)
    replay_state = FakeState()
    await handle_new_tasks(replay, replay_state, db, clock, config)
    assert len(await db.list_tasks(42, "open")) == 3
    assert any("уже" in item["text"].lower() for item in replay.answers)
    await db.close()


async def _past(path):
    db = await open_db(path / "tasks.sqlite3")
    clock = clock_at(2026, 11, 16, 12, 0)
    config = make_config(path / "tasks.sqlite3")
    state = FakeState()
    message = FakeMessage("20.01 купить бумагу", message_id=8)
    await handle_new_tasks(message, state, db, clock, config)
    assert await db.list_tasks(42, "open") == []
    assert state.state == Clarify.confirm_past.state
    assert "20.01.2026" in message.answers[-1]["text"]
    assert "2026" in message.answers[-1]["text"]
    assert "следующий год" in message.answers[-1]["text"]
    callback = FakeCallback("c:overdue:0", message)
    await on_clarify_callback(callback, state, db, clock, config, new_locks())
    task = (await db.list_tasks(42, "open"))[0]
    assert task.task_date == date(2026, 1, 20)
    assert task.task_date.year == 2026
    await db.close()


async def _invalid_and_ambiguous(path):
    db = await open_db(path / "tasks.sqlite3")
    clock = clock_at(2026, 11, 16, 12, 0)
    config = make_config(path / "tasks.sqlite3")
    state = FakeState()
    message = FakeMessage("31.02.2026 созвон\nв пятницу отправить отчёт", message_id=9)
    await handle_new_tasks(message, state, db, clock, config)
    assert await db.list_tasks(42, "open") == []
    joined = "\n".join(item["text"] for item in message.answers)
    assert "31.02.2026" in joined
    assert "точн" in joined
    callback = FakeCallback("c:tomorrow:0", message)
    await on_clarify_callback(callback, state, db, clock, config, new_locks())
    task = (await db.list_tasks(42, "open"))[0]
    assert task.task_date == date(2026, 11, 17)
    assert task.title == "в пятницу отправить отчёт"
    assert task.due_at_utc is None
    await db.close()


async def _cancel(path):
    db = await open_db(path / "tasks.sqlite3")
    clock = clock_at(2026, 11, 16, 12, 0)
    config = make_config(path / "tasks.sqlite3")
    state = FakeState()
    message = FakeMessage("завтра первое\nвторое", message_id=12)
    await handle_new_tasks(message, state, db, clock, config)
    assert len(await db.list_tasks(42, "open")) == 1
    callback = FakeCallback("c:cancel:0", message)
    await on_clarify_callback(callback, state, db, clock, config, new_locks())
    assert len(await db.list_tasks(42, "open")) == 1
    assert state.state is None
    assert "остановлен" in message.answers[-1]["text"].lower()
    assert "второе" in message.answers[-1]["text"]
    await db.close()


async def _done_and_delete(path):
    db = await open_db(path / "tasks.sqlite3")
    clock = clock_at(2026, 11, 16, 12, 0)
    config = make_config(path / "tasks.sqlite3")
    message = FakeMessage("сегодня позвонить", message_id=15)
    await handle_new_tasks(message, FakeState(), db, clock, config)
    task = (await db.list_tasks(42, "open"))[0]
    card = FakeMessage()
    done = FakeCallback(f"d:{task.id}:td:0", card)
    locks = new_locks()
    await on_done(done, db, clock, config, locks)
    finished = await db.get_task(42, task.id)
    assert finished.status == "done"
    first_completed = finished.completed_at
    later = clock_at(2026, 11, 16, 18, 0)
    again = FakeCallback(f"d:{task.id}:td:0", card)
    await on_done(again, db, later, config, locks)
    finished = await db.get_task(42, task.id)
    assert finished.status == "done"
    assert finished.completed_at == first_completed
    assert again.alerts[-1]["show_alert"] is True

    delete = FakeCallback(f"y:{task.id}", card)
    await on_delete_yes(delete, db, config, locks)
    assert await db.get_task(42, task.id) is None
    repeat = FakeCallback(f"y:{task.id}", card)
    await on_delete_yes(repeat, db, config, locks)
    assert repeat.alerts[-1]["show_alert"] is True
    assert "уже удалена" in repeat.alerts[-1]["text"].lower()
    await db.close()


async def _reschedule(path):
    db = await open_db(path / "tasks.sqlite3")
    clock = clock_at(2026, 11, 16, 12, 0)
    config = make_config(path / "tasks.sqlite3")
    message = FakeMessage("20.11.2026 в 15:00 позвонить подрядчику", message_id=21)
    await handle_new_tasks(message, FakeState(), db, clock, config)
    task = (await db.list_tasks(42, "open"))[0]
    assert from_utc(task.due_at_utc, clock.tz).time() == time(15, 0)
    state = FakeState()
    card = FakeMessage()
    locks = new_locks()
    start = FakeCallback(f"m:{task.id}:al:0", card)
    await on_move_start(start, state, db, clock, config, locks)
    assert "15:00" in card.answers[-1]["text"]
    assert (await db.get_task(42, task.id)).task_date == date(2026, 11, 20)

    choose = FakeCallback(f"r:tomorrow:{task.id}", card)
    await on_move_callback(choose, state, db, clock, config, locks)
    prompt = card.answers[-1]["text"]
    buttons = button_rows(card.answers[-1]["markup"])
    assert "Сейчас время: 15:00" in prompt
    assert ("Оставить 15:00", f"r:keep:{task.id}") in buttons
    assert (await db.get_task(42, task.id)).task_date == date(2026, 11, 20)

    keep = FakeCallback(f"r:keep:{task.id}", card)
    await on_move_callback(keep, state, db, clock, config, locks)
    moved = await db.get_task(42, task.id)
    assert moved.task_date == date(2026, 11, 17)
    assert from_utc(moved.due_at_utc, clock.tz).strftime("%H:%M") == "15:00"
    assert state.state is None

    stale = FakeCallback(f"r:keep:{task.id}", card)
    await on_move_callback(stale, state, db, clock, config, locks)
    assert stale.alerts[-1]["show_alert"] is True
    assert (await db.get_task(42, task.id)).task_date == date(2026, 11, 17)

    state = FakeState()
    card = FakeMessage()
    await on_move_start(FakeCallback(f"m:{task.id}:al:0", card), state, db, clock, config, locks)
    await on_move_callback(FakeCallback(f"r:today:{task.id}", card), state, db, clock, config, locks)
    await on_move_callback(FakeCallback(f"r:none:{task.id}", card), state, db, clock, config, locks)
    cleared = await db.get_task(42, task.id)
    assert cleared.task_date == date(2026, 11, 16)
    assert cleared.due_at_utc is None
    assert parse_exact_time("15:00") == time(15, 0)
    await db.close()


async def _edit_and_owner(path):
    db = await open_db(path / "tasks.sqlite3")
    clock = clock_at(2026, 11, 16, 12, 0)
    config = make_config(path / "tasks.sqlite3", user_id=42)
    message = FakeMessage("сегодня договор", message_id=30, user_id=1000)
    await handle_new_tasks(message, FakeState(), db, clock, config)
    task = (await db.get_task(42, 1))
    assert task.owner_id == 42
    assert await db.get_task(1000, task.id) is None
    state = FakeState()
    card = FakeMessage()
    locks = new_locks()
    await on_rename_start(FakeCallback(f"n:{task.id}:al:0", card), state, db, clock, config, locks)
    assert state.state == Rename.enter_title.state
    await on_rename_cancel(FakeCallback(f"e:cancel:{task.id}", card), state, db, clock, config, locks)
    assert state.state is None
    assert (await db.get_task(42, task.id)).title == "Договор"

    await on_rename_start(FakeCallback(f"n:{task.id}:al:0", card), state, db, clock, config, locks)
    renamed = FakeMessage("Новое название", message_id=31)
    await on_rename_text(renamed, state, db, clock, config, locks)
    updated = await db.get_task(42, task.id)
    assert updated.title == "Новое название"
    assert updated.task_date == date(2026, 11, 16)
    assert state.state is None

    extra = FakeMessage("завтра ещё одна", message_id=32)
    await handle_new_tasks(extra, state, db, clock, config)
    assert len(await db.list_tasks(42, "open")) == 2
    await db.close()
