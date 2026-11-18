"""Интеграционные проверки маршрутизации на настоящем Dispatcher.

Update подаются через feed_raw_update, исходящие запросы Telegram перехватывает
тестовая сессия. Токен настоящий не нужен, сеть и polling не используются.
"""

import asyncio
import itertools
from datetime import timedelta

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage

from taskbot import notes as notes_mod
from taskbot import recurrence as rec_mod
from taskbot.access import DENIED_TEXT
from taskbot.handlers import card, categories, export, fallback, intake, menu, notes, recur, search, settings, snooze
from taskbot.main import build_dispatcher
from tests.helpers import clock_at, make_config, new_locks, open_db

OWNER = 42
CHAT = 42
FAKE_TOKEN = "123456789:" + "A" * 35


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self._ids = itertools.count(1000)

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):
        payload = method.model_dump(exclude_none=True)
        self.calls.append((type(method).__name__, payload))
        if isinstance(method, (SendMessage, EditMessageText)):
            chat_id = payload.get("chat_id", CHAT)
            message_id = next(self._ids) if isinstance(method, SendMessage) else payload.get("message_id", 1)
            from aiogram.types import Message

            return Message.model_validate(
                {
                    "message_id": message_id,
                    "date": 1_800_000_000,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": 123456789, "is_bot": True, "first_name": "bot"},
                    "text": payload.get("text", ""),
                },
                context={"bot": bot},
            )
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)):
            return True
        return True


def _detach() -> None:
    for module in (card, categories, export, fallback, intake, menu, notes, recur, search, settings, snooze):
        module.router._parent_router = None


class Harness:
    def __init__(self, db, clock, config, locks) -> None:
        self.db, self.clock, self.config, self.locks = db, clock, config, locks
        self.session = FakeSession()
        self.bot = Bot(FAKE_TOKEN, session=self.session)
        # Роутеры обработчиков — модульные объекты и цепляются к родителю один раз за процесс.
        # В продакшене setup_router вызывается один раз; в тестах отцепляем перед новой сборкой.
        _detach()
        self.dispatcher = build_dispatcher(config)
        self.update_id = itertools.count(1)
        self.last_markup: dict[str, str] = {}  # текст кнопки -> callback_data последнего сообщения
        self.last_message_id = 999

    def _feed(self, update: dict):
        return self.dispatcher.feed_raw_update(
            self.bot, update, db=self.db, clock=self.clock, config=self.config, locks=self.locks
        )

    async def say(self, text: str, user_id: int = OWNER, chat_type: str = "private"):
        start = len(self.session.calls)
        await self._feed(
            {
                "update_id": next(self.update_id),
                "message": {
                    "message_id": next(self.update_id) + 5000,
                    "date": 1_800_000_000,
                    "chat": {"id": user_id, "type": chat_type},
                    "from": {"id": user_id, "is_bot": False, "first_name": "u"},
                    "text": text,
                },
            }
        )
        return self._collect(start)

    async def press(self, data: str, user_id: int = OWNER):
        start = len(self.session.calls)
        await self._feed(
            {
                "update_id": next(self.update_id),
                "callback_query": {
                    "id": str(next(self.update_id)),
                    "from": {"id": user_id, "is_bot": False, "first_name": "u"},
                    "chat_instance": "ci",
                    "data": data,
                    "message": {
                        "message_id": self.last_message_id,
                        "date": 1_800_000_000,
                        "chat": {"id": user_id, "type": "private"},
                        "from": {"id": 123456789, "is_bot": True, "first_name": "bot"},
                        "text": "старое сообщение",
                    },
                },
            }
        )
        return self._collect(start)

    def _collect(self, start: int) -> list[tuple[str, dict]]:
        new = self.session.calls[start:]
        for name, payload in reversed(new):
            if name in {"SendMessage", "EditMessageText"} and payload.get("reply_markup"):
                markup = payload["reply_markup"]
                rows = markup.get("inline_keyboard") or []
                self.last_markup = {
                    button["text"]: button.get("callback_data")
                    for row in rows
                    for button in row
                    if button.get("callback_data")
                }
                break
        return new

    async def press_button(self, label_part: str):
        for text, data in self.last_markup.items():
            if label_part in text:
                return await self.press(data)
        raise AssertionError(f"Нет кнопки «{label_part}» среди {list(self.last_markup)}")


def texts(calls) -> str:
    return "\n".join(p.get("text", "") for name, p in calls if name in {"SendMessage", "EditMessageText"})


def alerts(calls) -> list[dict]:
    return [p for name, p in calls if name == "AnswerCallbackQuery"]


def arun(function):
    def wrapper(tmp_path):
        async def body():
            db = await open_db(tmp_path / "r.sqlite3")
            try:
                harness = Harness(db, clock_at(2026, 11, 16, 10, 0), make_config(tmp_path / "r.sqlite3", OWNER), new_locks())
                await function(harness)
            finally:
                await db.close()
                _detach()

        return asyncio.run(body())

    wrapper.__name__ = function.__name__
    return wrapper


async def _series(harness, title="Зарядка", hour=None):
    from datetime import time

    rec = await rec_mod.create_recurrence(
        harness.db, OWNER, title=title, kind="daily", start_date=harness.clock.today(),
        time_local=time(hour, 0) if hour is not None else None, category_id=None,
        now=harness.clock.now_utc(),
    )
    await rec_mod.generate_for(harness.db, harness.clock, OWNER, rec.id)
    return rec


# ---------- заметки ----------


@arun
async def test_notes_entry_and_multiline_save(h):
    calls = await h.say("📝 Заметки")
    assert "Заметки" in texts(calls)
    await h.press_button("Добавить заметку")
    body = "купить хлеб завтра в 15:00\nвторая строка 20.11\n\nтретья"
    calls = await h.say(body)
    saved = await notes_mod.list_notes(h.db, OWNER)
    assert [n.text for n in saved] == [body]
    assert await h.db.list_tasks(OWNER, "open") == []
    # режим ввода закончился: обычный текст снова создаёт задачу, а не вторую заметку
    await h.say("позвонить маме 20.11")
    assert len(await notes_mod.list_notes(h.db, OWNER)) == 1
    assert len(await h.db.list_all_tasks(OWNER)) == 1


@arun
async def test_note_input_to_search_does_not_save(h):
    await h.say("📝 Заметки")
    await h.press_button("Добавить заметку")
    calls = await h.say("🔎 Поиск")
    assert await notes_mod.list_notes(h.db, OWNER) == []
    assert "Что найти" in texts(calls)
    await notes_mod.add_note(h.db, OWNER, "Идея про бюджет", h.clock.now_utc())
    calls = await h.say("БЮДЖЕТ")
    assert "бюджет" in texts(calls).lower()
    assert len(await notes_mod.list_notes(h.db, OWNER)) == 1
    assert await h.db.list_all_tasks(OWNER) == []


@arun
async def test_menu_button_during_search_is_not_a_query(h):
    await h.say("🔎 Поиск")
    calls = await h.say("📝 Заметки")
    assert "Заметки" in texts(calls)
    assert await h.db.list_all_tasks(OWNER) == []


@arun
async def test_note_to_task_with_date_choice(h):
    note = await notes_mod.add_note(h.db, OWNER, "Подготовить отчёт\nмного подробностей " * 5, h.clock.now_utc())
    await h.press(f"nt:t:{note.id}:n0")
    calls = await h.press("nt:tu")
    assert "дат" in texts(calls).lower()
    calls = await h.press_button("Завтра")
    tasks = await h.db.list_tasks(OWNER, "open")
    assert len(tasks) == 1 and tasks[0].note_id == note.id
    assert tasks[0].task_date == h.clock.today() + timedelta(days=1)
    assert (await notes_mod.get_note(h.db, OWNER, note.id)).text == note.text
    # повторное «Завтра» не создаёт вторую задачу
    await h.press("c:tomorrow:0")
    assert len(await h.db.list_all_tasks(OWNER)) == 1


# ---------- карточка и список ----------


@arun
async def test_card_callback_and_back_to_list(h):
    await h.say("позвонить врачу завтра")
    task = (await h.db.list_all_tasks(OWNER))[0]
    calls = await h.say("📋 Все задачи")
    assert "озвонить врачу" in texts(calls)
    await h.press_button("Позвонить")
    assert any("озвонить врачу" in p.get("text", "") for n, p in h.session.calls[-2:])
    assert any(d.startswith("l:") for d in h.last_markup.values())
    calls = await h.press_button("К списку")
    assert "озвонить врачу" in texts(calls)
    assert task.id


@arun
async def test_unknown_callback_and_stale_do_not_crash(h):
    for data in ("zzz", "o:al:0:9999", "nt:o:9999:n0", "rc:o:9999", "rc:ok", "nt:tu", "rc:es", "ex:nope"):
        calls = await h.press(data)
        assert alerts(calls), data


# ---------- серии ----------


@arun
async def test_series_choice_for_move_rename_delete(h):
    rec = await _series(h)
    first = (await h.db.list_tasks(OWNER, "open"))[1]
    ref = f"{first.id}:al:0"
    for action, single_prefix in (("m", "m1"), ("n", "n1")):
        calls = await h.press(f"{action}:{ref}")
        shown = texts(calls)
        assert "повторяющаяся" in shown
        assert set(h.last_markup) == {"Только эту задачу", "Изменить серию", "Отмена"}
        assert h.last_markup["Только эту задачу"].startswith(single_prefix + ":")
        assert h.last_markup["Изменить серию"] == f"rc:ed:{rec.id}"
        assert h.last_markup["Отмена"].startswith("z:")
    calls = await h.press(f"x:{ref}")
    assert set(h.last_markup) == {"Только эту задачу", "Удалить серию", "Отмена"}
    assert await h.db.get_task(OWNER, first.id) is not None  # без выбора ничего не удалено
    # «Отмена» возвращает карточку и ничего не меняет
    await h.press_button("Отмена")
    assert (await h.db.get_task(OWNER, first.id)).detached is False


@arun
async def test_move_single_detaches_and_original_not_regenerated(h):
    await _series(h)
    tasks = await h.db.list_tasks(OWNER, "open")
    target = tasks[1]
    original_day = target.task_date
    await h.press(f"m:{target.id}:al:0")
    await h.press_button("Только эту")
    await h.press_button("Послезавтра")  # выбор нового дня
    await h.press_button("Без времени")
    moved = await h.db.get_task(OWNER, target.id)
    assert moved.detached and moved.task_date != original_day
    assert moved.occurrence_date == original_day
    assert await rec_mod.generate_horizon(h.db, h.clock, OWNER) == 0
    same_day = [t for t in await h.db.list_all_tasks(OWNER) if t.occurrence_date == original_day]
    assert len(same_day) == 1


@arun
async def test_rename_single_keeps_series_untouched(h):
    rec = await _series(h)
    tasks = await h.db.list_tasks(OWNER, "open")
    await h.press(f"n:{tasks[2].id}:al:0")
    await h.press_button("Только эту")
    await h.say("Особая зарядка")
    assert (await h.db.get_task(OWNER, tasks[2].id)).title == "Особая зарядка"
    await rec_mod.update_template(
        h.db, h.clock, OWNER, rec.id, title="Новая", kind="daily", weekdays=(), month_day=None, time_local=None
    )
    titles = {t.id: t.title for t in await h.db.list_tasks(OWNER, "open")}
    assert titles[tasks[2].id] == "Особая зарядка" and titles[tasks[3].id] == "Новая"


@arun
async def test_edit_series_through_dialog_with_preview(h):
    rec = await _series(h, title="Зарядка", hour=7)
    tasks = await h.db.list_tasks(OWNER, "open")
    detached = tasks[3]
    await h.db.set_title(OWNER, detached.id, "Моя", h.clock.now_utc())
    await h.db.set_status(OWNER, tasks[0].id, "done", h.clock.now_utc())
    await h.press(f"m:{tasks[1].id}:al:0")
    calls = await h.press_button("Изменить серию")
    assert "название" in texts(calls).lower()
    await h.say("Бег")
    await h.press_button("Каждую неделю")
    await h.press("rc:w:0")
    await h.press("rc:w:2")
    await h.press("rc:wd")
    calls = await h.say("08:30")
    preview = texts(calls)
    assert "Бег" in preview and "пн, ср" in preview and "08:30" in preview
    assert "Не изменятся" in preview and "будут удалены" in preview
    # до сохранения ничего не изменилось
    assert (await rec_mod.get_recurrence(h.db, OWNER, rec.id)).title == "Зарядка"
    assert len(await h.db.list_tasks(OWNER, "open")) == 29
    await h.press("rc:es")
    again = await h.press("rc:es")
    assert alerts(again)[0].get("show_alert")
    new = await rec_mod.get_recurrence(h.db, OWNER, rec.id)
    assert new.kind == "weekly" and new.weekdays == (0, 2) and new.title == "Бег"
    everything = await h.db.list_all_tasks(OWNER)
    done = [t for t in everything if t.status == "done"]
    assert [t.id for t in done] == [tasks[0].id]
    assert (await h.db.get_task(OWNER, detached.id)).title == "Моя"
    open_series = [t for t in everything if t.status == "open" and not t.detached]
    assert open_series and all(t.title == "Бег" and t.task_date.weekday() in (0, 2) for t in open_series)
    assert all(t.due_at_utc is not None and t.due_at_utc.hour == 3 and t.due_at_utc.minute == 30 for t in open_series)
    dates = [t.occurrence_date for t in everything if t.recurrence_id == rec.id]
    assert len(dates) == len(set(dates))
    assert await rec_mod.generate_horizon(h.db, h.clock, OWNER) == 0


@arun
async def test_edit_series_cancel_changes_nothing(h):
    rec = await _series(h)
    before = [(t.id, t.title) for t in await h.db.list_all_tasks(OWNER)]
    await h.press(f"rc:ed:{rec.id}")
    await h.press_button("Оставить название")
    await h.press_button("Каждый месяц")
    await h.say("31")
    calls = await h.press_button("Без времени")
    assert "Проверьте" in texts(calls)
    await h.press_button("Отмена")
    assert [(t.id, t.title) for t in await h.db.list_all_tasks(OWNER)] == before
    assert (await rec_mod.get_recurrence(h.db, OWNER, rec.id)).kind == "daily"


@arun
async def test_edit_series_to_no_time_cancels_stale_reminders(h):
    from taskbot.schedule import run_tick
    from tests.test_schedule import FakeSender

    rec = await _series(h, hour=18)
    await run_tick(h.db, h.clock, h.config, FakeSender())
    assert await h.db.list_deliveries(OWNER, statuses=("pending",), kinds=("reminder",))
    await rec_mod.update_template(
        h.db, h.clock, OWNER, rec.id, title="Зарядка", kind="daily", weekdays=(), month_day=None, time_local=None
    )
    assert not await h.db.list_deliveries(OWNER, statuses=("pending",), kinds=("reminder",))
    await run_tick(h.db, h.clock, h.config, FakeSender())
    assert not await h.db.list_deliveries(OWNER, statuses=("pending",), kinds=("reminder",))
    assert all(t.due_at_utc is None for t in await h.db.list_tasks(OWNER, "open"))


# ---------- доступ ----------


@arun
async def test_stranger_denied_for_message_and_callback(h):
    note_before = await notes_mod.add_note(h.db, OWNER, "секрет", h.clock.now_utc())
    calls = await h.say("привет, добавь задачу", user_id=999)
    assert DENIED_TEXT in texts(calls)
    calls = await h.say("📝 Заметки", user_id=999)
    assert DENIED_TEXT in texts(calls) and "секрет" not in texts(calls)
    calls = await h.press(f"nt:d:{note_before.id}:n0", user_id=999)
    assert any(p.get("show_alert") for p in alerts(calls))
    calls = await h.press("ex:zip", user_id=999)
    assert not any(n == "SendDocument" for n, _ in calls)
    assert await h.db.list_all_tasks(OWNER) == []
    assert len(await notes_mod.list_notes(h.db, OWNER)) == 1
    # в групповом чате владельцу тоже отказ
    calls = await h.say("задача", user_id=OWNER, chat_type="group")
    assert await h.db.list_all_tasks(OWNER) == []


@arun
async def test_delete_single_instance_via_choice_is_not_regenerated(h):
    await _series(h)
    victim = (await h.db.list_tasks(OWNER, "open"))[2]
    await h.press(f"x:{victim.id}:al:0")
    await h.press_button("Только эту")
    assert await h.db.get_task(OWNER, victim.id) is None
    assert await rec_mod.generate_horizon(h.db, h.clock, OWNER) == 0
    assert len(await h.db.list_tasks(OWNER, "open")) == 29
