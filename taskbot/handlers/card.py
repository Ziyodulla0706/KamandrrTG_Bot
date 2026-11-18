from datetime import date, time

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from taskbot.config import Config
from taskbot.dates import Clock, from_utc, shift_day, to_utc
from taskbot.db import Database, Task
from taskbot.formatting import format_when, h
from taskbot.handlers.states import Clarify, Reschedule, Rename
from taskbot.handlers.view import CAT_ALL, safe_edit, show_card, show_list
from taskbot.keyboards import (
    EMPTY_INLINE,
    delete_keyboard,
    main_menu,
    series_choice_keyboard,
    move_cancel_keyboard,
    move_day_keyboard,
    move_past_keyboard,
    move_time_keyboard,
    rename_keyboard,
)
from taskbot.locks import UserLocks
from taskbot.parsing import INVALID_DATE, PAST, parse_exact_date, parse_exact_time
from taskbot.texts import (
    DATE_HINT,
    NOT_FOUND,
    SCOPE_ALL,
    SCOPE_DONE,
    SCOPE_SEARCH,
    SCOPE_TODAY,
    SCOPE_TOMORROW,
    SCOPE_WEEK,
    STALE,
    TIME_HINT,
)

router = Router(name="card")

_SCOPES = {SCOPE_TODAY, SCOPE_TOMORROW, SCOPE_ALL, SCOPE_DONE, SCOPE_WEEK, SCOPE_SEARCH}
_MOVE_STATES = (
    Reschedule.choose_day,
    Reschedule.enter_date,
    Reschedule.confirm_past,
    Reschedule.choose_time,
    Reschedule.enter_time,
)


@router.callback_query(F.data.startswith("o:"))
async def on_open(
    callback,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_open(callback.data or "")
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    scope, page, task_id = parsed
    async with locks.lock(config.allowed_user_id):
        task = await db.get_task(config.allowed_user_id, task_id)
        if task is None:
            await _missing(callback)
            return
        await callback.answer()
        await show_card(callback.message, task, clock, scope, page, edit=True, db=db)


@router.callback_query(F.data.startswith("l:"))
async def on_list(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_list(callback.data or "")
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    scope, page = parsed
    async with locks.lock(config.allowed_user_id):
        data = await state.get_data()
        if scope == SCOPE_SEARCH:
            from taskbot.handlers.search import show_results

            await callback.answer()
            await show_results(callback.message, db, clock, config, state, page, edit=True)
            return
        await callback.answer()
        await show_list(
            callback.message,
            db,
            clock,
            config,
            scope,
            page,
            edit=True,
            cat=str(data.get("tcat") or CAT_ALL),
        )


@router.callback_query(F.data == "noop")
async def on_noop(callback) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("d:"))
async def on_done(
    callback,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    await _change_status(callback, db, clock, config, locks, done=True)


@router.callback_query(F.data.startswith("u:"))
async def on_reopen(
    callback,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    await _change_status(callback, db, clock, config, locks, done=False)


@router.callback_query(F.data.startswith("x:"))
async def on_delete_ask(
    callback,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_ref(callback.data or "", "x")
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    task_id, scope, page = parsed
    async with locks.lock(config.allowed_user_id):
        task = await db.get_task(config.allowed_user_id, task_id)
        if task is None:
            await _missing(callback)
            return
        await callback.answer()
        if task.recurrence_id is not None:
            await safe_edit(
                callback.message,
                f"Это повторяющаяся задача.\n<b>{h(task.title)}</b>\n"
                f"{format_when(task, clock.tz)}\n\nЧто удалить?",
                series_choice_keyboard(
                    task.id, task.recurrence_id, scope, page, action="delete"
                ),
            )
            return
        await safe_edit(
            callback.message,
            f"Удалить задачу?\n<b>{h(task.title)}</b>\n{format_when(task, clock.tz)}",
            delete_keyboard(task.id, scope, page),
        )


@router.callback_query(F.data.startswith("y:"))
async def on_delete_yes(
    callback,
    db: Database,
    config: Config,
    locks: UserLocks,
) -> None:
    task_id = _parse_plain_id(callback.data or "", "y")
    if task_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        deleted = await db.delete_task(config.allowed_user_id, task_id)
        if not deleted:
            await callback.answer("Задача уже удалена.", show_alert=True)
            await safe_edit(callback.message, "Задача уже удалена.", EMPTY_INLINE)
            return
        await callback.answer("Задача удалена")
        await safe_edit(callback.message, "Задача удалена.", EMPTY_INLINE)


@router.callback_query(F.data.startswith("z:"))
async def on_delete_no(
    callback,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_ref(callback.data or "", "z")
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    task_id, scope, page = parsed
    async with locks.lock(config.allowed_user_id):
        task = await db.get_task(config.allowed_user_id, task_id)
        if task is None:
            await _missing(callback)
            return
        await callback.answer("Удаление отменено")
        await show_card(callback.message, task, clock, scope, page, edit=True, db=db)


@router.callback_query(F.data.startswith("m:"))
async def on_move_start(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    await _move_start(callback, state, db, clock, config, locks, "m")


@router.callback_query(F.data.startswith("m1:"))
async def on_move_single(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    await _move_start(callback, state, db, clock, config, locks, "m1")


async def _move_start(callback, state, db, clock, config, locks, prefix: str) -> None:
    parsed = _parse_ref(callback.data or "", prefix)
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    task_id, scope, page = parsed
    async with locks.lock(config.allowed_user_id):
        if await _busy_with_clarify(callback, state):
            return
        task = await db.get_task(config.allowed_user_id, task_id)
        if task is None:
            await _missing(callback)
            return
        if prefix == "m" and task.recurrence_id is not None:
            await callback.answer()
            await callback.message.answer(
                f"Это повторяющаяся задача.\n<b>{h(task.title)}</b>\n\nЧто перенести?",
                reply_markup=series_choice_keyboard(
                    task.id, task.recurrence_id, scope, page, action="move"
                ),
            )
            return
        await state.set_data(_move_data(task, scope, page, clock))
        await state.set_state(Reschedule.choose_day)
        await callback.answer()
        await callback.message.answer(
            f"Сейчас: {format_when(task, clock.tz)}\nВыберите новый день.",
            reply_markup=move_day_keyboard(task.id),
        )


@router.callback_query(F.data.startswith("n:"))
async def on_rename_start(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    await _rename_start(callback, state, db, clock, config, locks, "n")


@router.callback_query(F.data.startswith("n1:"))
async def on_rename_single(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    await _rename_start(callback, state, db, clock, config, locks, "n1")


async def _rename_start(callback, state, db, clock, config, locks, prefix: str) -> None:
    parsed = _parse_ref(callback.data or "", prefix)
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    task_id, scope, page = parsed
    async with locks.lock(config.allowed_user_id):
        if await _busy_with_clarify(callback, state):
            return
        task = await db.get_task(config.allowed_user_id, task_id)
        if task is None:
            await _missing(callback)
            return
        if prefix == "n" and task.recurrence_id is not None:
            await callback.answer()
            await callback.message.answer(
                f"Это повторяющаяся задача.\n<b>{h(task.title)}</b>\n\nЧто изменить?",
                reply_markup=series_choice_keyboard(
                    task.id, task.recurrence_id, scope, page, action="rename"
                ),
            )
            return
        await state.set_data({"task_id": task.id, "scope": scope, "page": page})
        await state.set_state(Rename.enter_title)
        await callback.answer()
        await callback.message.answer(
            "Отправьте новое название задачи.\n"
            "Дата и время не изменятся — для них есть «Перенести».\n\n"
            f"Сейчас: <b>{h(task.title)}</b>",
            reply_markup=rename_keyboard(task.id),
        )


@router.callback_query(F.data.startswith("e:cancel:"))
async def on_rename_cancel(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    task_id = _parse_plain_id((callback.data or "").replace("e:cancel:", "e:", 1), "e")
    if task_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        data = await state.get_data()
        if await state.get_state() != Rename.enter_title.state or int(data.get("task_id") or 0) != task_id:
            await callback.answer(STALE, show_alert=True)
            return
        scope = str(data.get("scope") or SCOPE_ALL)
        page = int(data.get("page") or 0)
        await state.clear()
        task = await db.get_task(config.allowed_user_id, task_id)
        await callback.answer("Изменение отменено")
        if task is None:
            await _missing(callback)
            return
        await show_card(
            callback.message,
            task,
            clock,
            scope,
            page,
            edit=False,
            notice="Изменение отменено. Название осталось прежним.",
            db=db,
        )


@router.message(StateFilter(Rename.enter_title), F.text)
async def on_rename_text(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != Rename.enter_title.state:
            return
        data = await state.get_data()
        task_id = int(data.get("task_id") or 0)
        scope = str(data.get("scope") or SCOPE_ALL)
        page = int(data.get("page") or 0)
        title = " ".join((message.text or "").split())
        if not title:
            await message.answer(
                "Название не может быть пустым.",
                reply_markup=rename_keyboard(task_id),
            )
            return
        if len(title) > 1000:
            await message.answer(
                "Название длиннее 1000 символов. Сократите его.",
                reply_markup=rename_keyboard(task_id),
            )
            return
        updated = await db.set_title(config.allowed_user_id, task_id, title, clock.now_utc())
        await state.clear()
        if updated is None:
            await message.answer(NOT_FOUND, reply_markup=main_menu())
            return
        await show_card(
            message,
            updated,
            clock,
            scope,
            page,
            edit=False,
            notice="Название обновлено",
            db=db,
        )


@router.callback_query(StateFilter(*_MOVE_STATES), F.data.startswith("r:"))
async def on_move_callback(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_action_id(callback.data or "", "r")
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    action, task_id = parsed
    async with locks.lock(config.allowed_user_id):
        current = await state.get_state()
        data = await state.get_data()
        if current not in {item.state for item in _MOVE_STATES} or int(data.get("task_id") or 0) != task_id:
            await callback.answer(STALE, show_alert=True)
            return
        if action == "cancel":
            await _cancel_move(callback, state, db, clock, config, data)
            return
        if action == "other" and current == Reschedule.choose_day.state:
            await state.set_state(Reschedule.enter_date)
            await callback.answer()
            await callback.message.answer(DATE_HINT, reply_markup=move_cancel_keyboard(task_id))
            return
        if action == "fix" and current == Reschedule.confirm_past.state:
            await state.set_state(Reschedule.enter_date)
            await callback.answer()
            await callback.message.answer(DATE_HINT, reply_markup=move_cancel_keyboard(task_id))
            return
        if action == "overdue" and current == Reschedule.confirm_past.state:
            new_date = _stored_date(data)
            if new_date is None or new_date >= clock.today():
                await callback.answer(STALE, show_alert=True)
                return
            data["allow_past"] = True
            await _ask_time(callback.message, state, data, new_date)
            await callback.answer()
            return
        if action in {"today", "tomorrow", "after"} and current in {
            Reschedule.choose_day.state,
            Reschedule.enter_date.state,
        }:
            offset = {"today": 0, "tomorrow": 1, "after": 2}[action]
            await _take_day(callback.message, callback, state, data, shift_day(clock.today(), offset), clock)
            return
        if current == Reschedule.choose_time.state and action in {"keep", "none", "set"}:
            await _take_time_action(callback, state, db, clock, config, data, action)
            return
        await callback.answer(STALE, show_alert=True)


@router.message(StateFilter(Reschedule.enter_date), F.text)
async def on_move_date_text(
    message,
    state: FSMContext,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != Reschedule.enter_date.state:
            return
        data = await state.get_data()
        task_id = int(data.get("task_id") or 0)
        parsed = parse_exact_date(message.text or "", today=clock.today())
        if parsed.kind == INVALID_DATE or parsed.task_date is None:
            await message.answer(
                parsed.message or DATE_HINT,
                reply_markup=move_cancel_keyboard(task_id),
            )
            return
        if parsed.kind == PAST:
            data["new_date"] = parsed.task_date.isoformat()
            data["allow_past"] = False
            await state.set_data(data)
            await state.set_state(Reschedule.confirm_past)
            extra = ""
            if parsed.year_inferred:
                extra = (
                    f"\nГод не был указан, поэтому беру {parsed.task_date.year} "
                    "и не переношу дату на следующий год."
                )
            await message.answer(
                f"Дата {parsed.task_date.strftime('%d.%m.%Y')} уже прошла.{extra}\n"
                "Исправьте её или сохраните задачу как просроченную.",
                reply_markup=move_past_keyboard(task_id),
            )
            return
        await _take_day(message, None, state, data, parsed.task_date, clock)


@router.message(StateFilter(Reschedule.enter_time), F.text)
async def on_move_time_text(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != Reschedule.enter_time.state:
            return
        data = await state.get_data()
        task_id = int(data.get("task_id") or 0)
        parsed = parse_exact_time(message.text or "")
        if parsed is None:
            await message.answer(
                f"Не понял время. {TIME_HINT}",
                reply_markup=move_cancel_keyboard(task_id),
            )
            return
        await _apply_schedule(message, None, state, db, clock, config, data, parsed)


@router.message(StateFilter(Reschedule))
async def on_move_other(message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = int(data.get("task_id") or 0)
    await message.answer(
        "Продолжите перенос кнопкой или отправьте дату/время, если бот об этом попросил. "
        "Отмена — кнопка «Отмена» или /cancel.",
        reply_markup=move_cancel_keyboard(task_id),
    )


@router.message(StateFilter(Rename))
async def on_rename_other(message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.answer(
        "Отправьте новое название текстом или нажмите «Отмена».",
        reply_markup=rename_keyboard(int(data.get("task_id") or 0)),
    )


async def _change_status(callback, db, clock, config, locks, *, done: bool) -> None:
    prefix = "d" if done else "u"
    parsed = _parse_ref(callback.data or "", prefix)
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    task_id, scope, page = parsed
    async with locks.lock(config.allowed_user_id):
        task = await db.get_task(config.allowed_user_id, task_id)
        if task is None:
            await _missing(callback)
            return
        if done and task.status == "done":
            await callback.answer("Задача уже выполнена.", show_alert=True)
            await show_card(callback.message, task, clock, scope, page, edit=True, db=db)
            return
        if not done and task.status == "open":
            await callback.answer("Задача уже в работе.", show_alert=True)
            await show_card(callback.message, task, clock, scope, page, edit=True, db=db)
            return
        updated = await db.set_status(
            config.allowed_user_id,
            task_id,
            "done" if done else "open",
            clock.now_utc(),
        )
        if updated is None:
            await _missing(callback)
            return
        await callback.answer("Задача выполнена" if done else "Задача снова в работе")
        await show_card(callback.message, updated, clock, scope, page, edit=True, db=db)


async def _take_day(reply_to, callback, state, data, new_date: date, clock: Clock) -> None:
    if new_date < clock.today():
        data["new_date"] = new_date.isoformat()
        data["allow_past"] = False
        await state.set_data(data)
        await state.set_state(Reschedule.confirm_past)
        if callback is not None:
            await callback.answer()
        await reply_to.answer(
            f"Дата {new_date.strftime('%d.%m.%Y')} уже прошла.\n"
            "Исправьте её или сохраните задачу как просроченную.",
            reply_markup=move_past_keyboard(int(data["task_id"])),
        )
        return
    if callback is not None:
        await callback.answer()
    await _ask_time(reply_to, state, data, new_date)


async def _ask_time(reply_to, state, data: dict, new_date: date) -> None:
    data["new_date"] = new_date.isoformat()
    await state.set_data(data)
    await state.set_state(Reschedule.choose_time)
    old_time = data.get("old_time")
    if old_time:
        hint = (
            f"Новый день: {new_date.strftime('%d.%m.%Y')}\n"
            f"Сейчас время: {old_time}.\n"
            "Оставьте его, укажите другое или уберите."
        )
    else:
        hint = (
            f"Новый день: {new_date.strftime('%d.%m.%Y')}\n"
            "Сейчас у задачи нет времени.\n"
            "Можно указать время или сохранить без времени."
        )
    await reply_to.answer(hint, reply_markup=move_time_keyboard(int(data["task_id"]), old_time))


async def _take_time_action(callback, state, db, clock, config, data, action: str) -> None:
    if action == "set":
        await state.set_state(Reschedule.enter_time)
        await callback.answer()
        await callback.message.answer(TIME_HINT, reply_markup=move_cancel_keyboard(int(data["task_id"])))
        return
    if action == "keep":
        old_time = data.get("old_time")
        if not old_time:
            await callback.answer(STALE, show_alert=True)
            return
        parsed = parse_exact_time(old_time)
        if parsed is None:
            await callback.answer(STALE, show_alert=True)
            return
        await _apply_schedule(callback.message, callback, state, db, clock, config, data, parsed)
        return
    await _apply_schedule(callback.message, callback, state, db, clock, config, data, None)


async def _apply_schedule(
    reply_to,
    callback,
    state,
    db: Database,
    clock: Clock,
    config: Config,
    data: dict,
    clock_time: time | None,
) -> None:
    if await state.get_state() not in {Reschedule.choose_time.state, Reschedule.enter_time.state}:
        if callback is not None:
            await callback.answer(STALE, show_alert=True)
        return
    new_date = _stored_date(data)
    if new_date is None:
        if callback is not None:
            await callback.answer(STALE, show_alert=True)
        return
    if new_date < clock.today() and not data.get("allow_past"):
        if callback is not None:
            await callback.answer(STALE, show_alert=True)
        return
    due = to_utc(new_date, clock_time, clock.tz) if clock_time is not None else None
    updated = await db.set_schedule(
        config.allowed_user_id,
        int(data["task_id"]),
        new_date,
        due,
        clock.now_utc(),
    )
    scope = str(data.get("scope") or SCOPE_ALL)
    page = int(data.get("page") or 0)
    await state.clear()
    if updated is None:
        if callback is not None:
            await callback.answer(NOT_FOUND, show_alert=True)
        await reply_to.answer(NOT_FOUND, reply_markup=main_menu())
        return
    if callback is not None:
        await callback.answer("Задача перенесена")
    await show_card(
        reply_to,
        updated,
        clock,
        scope,
        page,
        edit=False,
        notice="Задача перенесена",
        db=db,
    )


async def _cancel_move(callback, state, db, clock, config, data) -> None:
    scope = str(data.get("scope") or SCOPE_ALL)
    page = int(data.get("page") or 0)
    task_id = int(data.get("task_id") or 0)
    await state.clear()
    task = await db.get_task(config.allowed_user_id, task_id)
    await callback.answer("Перенос отменён")
    if task is None:
        await _missing(callback)
        return
    await show_card(
        callback.message,
        task,
        clock,
        scope,
        page,
        edit=False,
        notice="Перенос отменён. Дата и время остались прежними.",
        db=db,
    )


async def _busy_with_clarify(callback, state: FSMContext) -> bool:
    current = await state.get_state()
    if current and current.startswith(Clarify.__name__):
        await callback.answer(
            "Сначала закончите добавление задачи или нажмите /cancel.",
            show_alert=True,
        )
        return True
    return False


async def _missing(callback) -> None:
    await callback.answer(NOT_FOUND, show_alert=True)
    if getattr(callback, "message", None) is not None:
        await safe_edit(callback.message, NOT_FOUND, EMPTY_INLINE)


def _move_data(task: Task, scope: str, page: int, clock: Clock) -> dict:
    old_time = None
    if task.due_at_utc is not None:
        old_time = from_utc(task.due_at_utc, clock.tz).strftime("%H:%M")
    return {
        "task_id": task.id,
        "scope": scope,
        "page": page,
        "new_date": None,
        "old_time": old_time,
        "allow_past": False,
    }


def _stored_date(data: dict) -> date | None:
    raw = data.get("new_date")
    if not raw:
        return None
    return date.fromisoformat(raw)


def _parse_ref(data: str, prefix: str) -> tuple[int, str, int] | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != prefix or parts[2] not in _SCOPES:
        return None
    try:
        task_id = int(parts[1])
        page = int(parts[3])
    except ValueError:
        return None
    if page < 0:
        return None
    return task_id, parts[2], page


def _parse_open(data: str) -> tuple[str, int, int] | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "o" or parts[1] not in _SCOPES:
        return None
    try:
        page = int(parts[2])
        task_id = int(parts[3])
    except ValueError:
        return None
    if page < 0:
        return None
    return parts[1], page, task_id


def _parse_list(data: str) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "l" or parts[1] not in _SCOPES:
        return None
    try:
        page = int(parts[2])
    except ValueError:
        return None
    if page < 0:
        return None
    return parts[1], page


def _parse_plain_id(data: str, prefix: str) -> int | None:
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != prefix:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _parse_action_id(data: str, prefix: str) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != prefix:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None
