from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext

from taskbot.config import Config
from taskbot.dates import Clock
from taskbot.db import Database
from taskbot.formatting import batch_summary
from taskbot.handlers.view import show_list
from taskbot.keyboards import main_menu
from taskbot.locks import UserLocks
from taskbot.texts import (
    ADD_HELP,
    BTN_ADD,
    BTN_ALL,
    BTN_DONE,
    BTN_EXPORT,
    BTN_NOTES,
    BTN_RECUR,
    BTN_SEARCH,
    BTN_TODAY,
    BTN_TOMORROW,
    BTN_CATS,
    BTN_WEEK,
    MENU_TO_SCOPE,
    SECTION_BUTTONS,
    START,
    STOPPED,
)

router = Router(name="menu")


@router.message(CommandStart())
async def on_start(message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(START, reply_markup=main_menu())


@router.message(Command("cancel"))
async def on_cancel(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        data = await state.get_data()
        active = await state.get_state()
        await state.clear()
        if not active:
            await message.answer("Сейчас нет активного диалога.", reply_markup=main_menu())
            return
        queue = data.get("queue") or []
        index = int(data.get("index") or 0)
        skipped = queue[index:]
        saved = await _load_tasks(db, config, data.get("saved_ids") or [])
        already = await _load_tasks(db, config, data.get("already_ids") or [])
        if skipped or saved or already or data.get("rejected"):
            text = batch_summary(
                saved=saved,
                already=already,
                rejected=data.get("rejected") or [],
                tz=clock.tz,
                skipped=skipped,
                heading=STOPPED,
            )
        else:
            text = STOPPED
        await message.answer(text, reply_markup=main_menu())


@router.message(F.text == BTN_ADD)
async def on_add(message, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        data = await state.get_data()
        active = await state.get_state()
        await state.clear()
        prefix = ""
        if active and data.get("queue"):
            prefix = "Предыдущее уточнение остановлено. Незавершённые строки не сохранены.\n\n"
        await message.answer(prefix + ADD_HELP, reply_markup=main_menu())


@router.message(F.text.in_({BTN_TODAY, BTN_TOMORROW, BTN_ALL, BTN_DONE}))
async def on_list_button(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        data = await state.get_data()
        active = await state.get_state()
        await state.clear()
        if active and data.get("queue"):
            await message.answer(
                "Предыдущее уточнение остановлено. Незавершённые строки не сохранены."
            )
        await show_list(
            message,
            db,
            clock,
            config,
            MENU_TO_SCOPE[message.text],
            0,
            edit=False,
        )


@router.message(F.text.in_(set(SECTION_BUTTONS)))
async def on_section_button(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    """Кнопки разделов. Всегда сбрасывают диалог, поэтому не становятся чужим вводом."""

    from taskbot.handlers import categories, export, notes, recur, search
    from taskbot.handlers.view import show_week

    async with locks.lock(config.allowed_user_id):
        data = await state.get_data()
        active = await state.get_state()
        await state.clear()
        if active and data.get("queue"):
            await message.answer(
                "Предыдущее уточнение остановлено. Незавершённые строки не сохранены."
            )
        label = message.text
        if label == BTN_NOTES:
            await notes.open_notes(message, state, db, clock, config)
        elif label == BTN_SEARCH:
            await search.open_search(message, state)
        elif label == BTN_WEEK:
            await show_week(message, db, clock, config, 0, edit=False)
        elif label == BTN_RECUR:
            await recur.show_series_list(message, db, clock, config, 0, edit=False)
        elif label == BTN_CATS:
            await categories.show_categories(message, db, clock, config, edit=False)
        elif label == BTN_EXPORT:
            await export.show_export(message)


@router.message(F.text.startswith("/"))
async def on_unknown_command(message, state: FSMContext) -> None:
    active = await state.get_state()
    await state.clear()
    text = "Неизвестная команда. Нажмите /start."
    if active:
        text = "Диалог остановлен.\n" + text
    await message.answer(text, reply_markup=main_menu())


async def _load_tasks(db: Database, config: Config, ids: list[int]) -> list:
    tasks = []
    for task_id in ids:
        task = await db.get_task(config.allowed_user_id, int(task_id))
        if task is not None:
            tasks.append(task)
    return tasks
