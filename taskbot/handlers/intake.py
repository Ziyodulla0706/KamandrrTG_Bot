from datetime import date

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from taskbot.config import Config
from taskbot.dates import Clock, shift_day
from taskbot.db import Database
from taskbot.formatting import batch_summary, confirmation, question_text
from taskbot.handlers.states import Clarify
from taskbot.intake import persist_ready, prepare_message, save_payload
from taskbot.keyboards import (
    clarify_cancel_keyboard,
    date_choice_keyboard,
    main_menu,
    past_keyboard,
    saved_keyboard,
)
from taskbot.locks import UserLocks
from taskbot.parsing import INVALID_DATE, PAST, parse_exact_date
from taskbot.texts import DATE_HINT, STALE, STOPPED

router = Router(name="intake")

_CLARIFY_STATES = (
    Clarify.choose_date,
    Clarify.enter_date,
    Clarify.confirm_past,
)


@router.callback_query(StateFilter(*_CLARIFY_STATES), F.data.startswith("c:"))
async def on_clarify_callback(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_clarify(callback.data or "")
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    action, index = parsed
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() not in {item.state for item in _CLARIFY_STATES}:
            await callback.answer(STALE, show_alert=True)
            return
        data = await state.get_data()
        if int(data.get("index") or 0) != index or _current(data) is None:
            await callback.answer(STALE, show_alert=True)
            return
        if action == "cancel":
            await _cancel(callback, state, db, clock, config, data)
            return
        if action == "other":
            await state.set_state(Clarify.enter_date)
            await callback.answer()
            await callback.message.answer(DATE_HINT, reply_markup=clarify_cancel_keyboard(index))
            return
        if action == "fix":
            if await state.get_state() != Clarify.confirm_past.state:
                await callback.answer(STALE, show_alert=True)
                return
            await state.set_state(Clarify.enter_date)
            await callback.answer()
            await callback.message.answer(DATE_HINT, reply_markup=clarify_cancel_keyboard(index))
            return
        if action == "overdue":
            if await state.get_state() != Clarify.confirm_past.state:
                await callback.answer(STALE, show_alert=True)
                return
            item = _current(data)
            if not item or not item.get("proposed_date"):
                await callback.answer(STALE, show_alert=True)
                return
            task_date = date.fromisoformat(item["proposed_date"])
            if task_date >= clock.today():
                await callback.answer("Эта дата ещё не прошла.", show_alert=True)
                return
            await _save_and_advance(
                callback.message,
                callback,
                state,
                db,
                clock,
                config,
                data,
                task_date,
            )
            return
        if action in {"today", "tomorrow"}:
            task_date = clock.today() if action == "today" else shift_day(clock.today(), 1)
            await _save_and_advance(
                callback.message,
                callback,
                state,
                db,
                clock,
                config,
                data,
                task_date,
            )
            return
        await callback.answer(STALE, show_alert=True)


@router.message(StateFilter(Clarify.enter_date), F.text)
async def on_enter_date(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != Clarify.enter_date.state:
            return
        await _accept_typed_date(message, state, db, clock, config, allow_reminder=False)


@router.message(StateFilter(Clarify.choose_date), F.text)
async def on_choose_date_text(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != Clarify.choose_date.state:
            return
        await _accept_typed_date(message, state, db, clock, config, allow_reminder=True)


@router.message(StateFilter(Clarify.confirm_past), F.text)
async def on_past_text(message, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != Clarify.confirm_past.state:
            return
        data = await state.get_data()
        await message.answer(
            "Нажмите «Исправить», «Сохранить как просроченную» или «Отмена».",
            reply_markup=past_keyboard(int(data.get("index") or 0)),
        )


@router.message(StateFilter(None), F.text)
async def on_new_tasks(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() is not None:
            return
        await handle_new_tasks(message, state, db, clock, config)


@router.message(StateFilter(Clarify))
async def on_clarify_other(message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.answer(
        "Сейчас нужна дата или кнопка под сообщением. Отмена — кнопка «Отмена» или /cancel.",
        reply_markup=clarify_cancel_keyboard(int(data.get("index") or 0)),
    )


async def handle_new_tasks(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    note_id: int | None = None,
) -> None:
    prepared = prepare_message(message.text or "", today=clock.today())
    if not prepared.ready and not prepared.queue and not prepared.rejected:
        await message.answer("Пришлите задачу текстом.", reply_markup=main_menu())
        return
    chat_id = message.chat.id
    message_id = message.message_id
    saved, already = await persist_ready(
        db, config, clock, prepared.ready, chat_id, message_id, note_id
    )
    single = (
        not prepared.queue
        and not prepared.rejected
        and len(saved) + len(already) == 1
    )
    if single:
        task = saved[0] if saved else already[0]
        heading = "Задача сохранена" if saved else "Эта задача уже сохранена"
        await state.clear()
        await message.answer(
            confirmation(heading, task, clock.tz),
            reply_markup=saved_keyboard([task]),
        )
        return

    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "saved_ids": [task.id for task in saved],
        "already_ids": [task.id for task in already],
        "rejected": prepared.rejected,
        "queue": prepared.queue,
        "index": 0,
        "note_id": note_id,
    }
    if saved or already or prepared.rejected:
        markup = saved_keyboard(saved or already) if (saved or already) else main_menu()
        await message.answer(
            batch_summary(
                saved=saved,
                already=already,
                rejected=prepared.rejected,
                tz=clock.tz,
            ),
            reply_markup=markup,
        )
    if prepared.queue:
        await state.set_data(data)
        await _ask_current(message, state, data)
        return
    await state.clear()


async def _accept_typed_date(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    *,
    allow_reminder: bool,
) -> None:
    data = await state.get_data()
    item = _current(data)
    if item is None:
        await state.clear()
        await message.answer(STALE, reply_markup=main_menu())
        return
    parsed = parse_exact_date(message.text or "", today=clock.today())
    if parsed.kind == INVALID_DATE:
        if allow_reminder:
            await message.answer(
                "Выберите кнопку под сообщением или отправьте только дату. "
                "Отмена — кнопка «Отмена».",
                reply_markup=date_choice_keyboard(int(data["index"])),
            )
            return
        await message.answer(parsed.message, reply_markup=clarify_cancel_keyboard(int(data["index"])))
        return
    if parsed.task_date is None:
        await message.answer(parsed.message or DATE_HINT)
        return
    if parsed.kind == PAST:
        item["proposed_date"] = parsed.task_date.isoformat()
        item["year_inferred"] = parsed.year_inferred
        item["kind"] = "past"
        data["queue"][int(data["index"])] = item
        await state.set_data(data)
        await state.set_state(Clarify.confirm_past)
        await message.answer(
            question_text(item, int(data["index"]), len(data["queue"])),
            reply_markup=past_keyboard(int(data["index"])),
        )
        return
    await _save_and_advance(
        message,
        None,
        state,
        db,
        clock,
        config,
        data,
        parsed.task_date,
    )


async def _save_and_advance(
    reply_to,
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    data: dict,
    task_date: date,
) -> None:
    item = _current(data)
    if item is None:
        await state.clear()
        if callback is not None:
            await callback.answer(STALE, show_alert=True)
        return
    task, created = await save_payload(
        db,
        config,
        clock,
        item,
        task_date,
        int(data["chat_id"]),
        int(data["message_id"]),
        data.get("note_id"),
    )
    if created:
        data.setdefault("saved_ids", []).append(task.id)
        heading = "Задача сохранена"
        toast = "Задача сохранена"
    else:
        data.setdefault("already_ids", []).append(task.id)
        heading = "Эта задача уже сохранена"
        toast = "Уже была сохранена"
    if callback is not None:
        await callback.answer(toast)
    await reply_to.answer(confirmation(heading, task, clock.tz), reply_markup=saved_keyboard([task]))
    data["index"] = int(data["index"]) + 1
    await state.set_data(data)
    if _current(data) is None:
        await state.clear()
        await reply_to.answer("Уточнений больше нет.", reply_markup=main_menu())
        return
    await _ask_current(reply_to, state, data)


async def _ask_current(reply_to, state: FSMContext, data: dict) -> None:
    item = _current(data)
    if item is None:
        await state.clear()
        return
    index = int(data["index"])
    text = question_text(item, index, len(data["queue"]))
    await state.set_data(data)
    if item["kind"] == "past":
        await state.set_state(Clarify.confirm_past)
        markup = past_keyboard(index)
    else:
        await state.set_state(Clarify.choose_date)
        markup = date_choice_keyboard(index)
    await reply_to.answer(text, reply_markup=markup)


async def _cancel(callback, state: FSMContext, db: Database, clock: Clock, config: Config, data: dict) -> None:
    index = int(data.get("index") or 0)
    skipped = (data.get("queue") or [])[index:]
    saved = []
    already = []
    for task_id in data.get("saved_ids") or []:
        task = await db.get_task(config.allowed_user_id, int(task_id))
        if task is not None:
            saved.append(task)
    for task_id in data.get("already_ids") or []:
        task = await db.get_task(config.allowed_user_id, int(task_id))
        if task is not None:
            already.append(task)
    await state.clear()
    text = batch_summary(
        saved=saved,
        already=already,
        rejected=data.get("rejected") or [],
        tz=clock.tz,
        skipped=skipped,
        heading=STOPPED,
    )
    await callback.answer("Диалог остановлен")
    await callback.message.answer(text, reply_markup=main_menu())


def _current(data: dict) -> dict | None:
    queue = data.get("queue") or []
    index = int(data.get("index") or 0)
    if index < 0 or index >= len(queue):
        return None
    return queue[index]


def _parse_clarify(data: str) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "c":
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None
