from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from taskbot.config import Config
from taskbot.dates import Clock
from taskbot.db import REMINDER_LEADS, Database
from taskbot.handlers.states import Clarify, SettingsDialog
from taskbot.handlers.view import safe_edit
from taskbot.notify import digest_time_keyboard, render_settings, settings_keyboard
from taskbot.locks import UserLocks
from taskbot.parsing import parse_exact_time
from taskbot.schedule import sync_plan
from taskbot.texts import BTN_SETTINGS, STALE

router = Router(name="settings")


@router.message(F.text == BTN_SETTINGS)
async def on_settings(
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
        settings = await db.ensure_settings(config.allowed_user_id, clock.now_utc())
        prefix = ""
        if active and data.get("queue"):
            prefix = "Предыдущее уточнение остановлено. Незавершённые строки не сохранены.\n\n"
        await message.answer(
            prefix + render_settings(settings, config.timezone.key),
            reply_markup=settings_keyboard(settings),
        )


@router.callback_query(F.data == "st:digest")
async def on_toggle_digest(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    if await _blocked_by_clarify(callback, state):
        return
    async with locks.lock(config.allowed_user_id):
        await state.set_state(None)
        settings = await db.ensure_settings(config.allowed_user_id, clock.now_utc())
        settings.digest_enabled = not settings.digest_enabled
        stored = await db.save_settings(settings, clock.now_utc())
        await sync_plan(db, clock, config)
        await callback.answer("Сводка включена" if stored.digest_enabled else "Сводка выключена")
        await _show(callback.message, stored, config, edit=True)


@router.callback_query(F.data == "st:remind")
async def on_toggle_reminders(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    if await _blocked_by_clarify(callback, state):
        return
    async with locks.lock(config.allowed_user_id):
        await state.set_state(None)
        settings = await db.ensure_settings(config.allowed_user_id, clock.now_utc())
        settings.reminders_enabled = not settings.reminders_enabled
        stored = await db.save_settings(settings, clock.now_utc())
        await sync_plan(db, clock, config)
        await callback.answer(
            "Напоминания включены" if stored.reminders_enabled else "Напоминания выключены"
        )
        await _show(callback.message, stored, config, edit=True)


@router.callback_query(F.data == "st:time")
async def on_digest_time_start(
    callback,
    state: FSMContext,
    locks: UserLocks,
    config: Config,
) -> None:
    if await _blocked_by_clarify(callback, state):
        return
    async with locks.lock(config.allowed_user_id):
        await state.set_state(SettingsDialog.enter_time)
        await callback.answer()
        await callback.message.answer(
            "Отправьте время сводки в виде ЧЧ:ММ, например 09:00.",
            reply_markup=digest_time_keyboard(),
        )


@router.callback_query(F.data == "st:time:cancel")
async def on_digest_time_cancel(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        await state.clear()
        settings = await db.ensure_settings(config.allowed_user_id, clock.now_utc())
        await callback.answer("Ввод отменён")
        await _show(callback.message, settings, config, edit=True)


@router.callback_query(F.data.startswith("st:lead:"))
async def on_lead(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    if await _blocked_by_clarify(callback, state):
        return
    try:
        minutes = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        minutes = -1
    if minutes not in REMINDER_LEADS:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        await state.set_state(None)
        settings = await db.ensure_settings(config.allowed_user_id, clock.now_utc())
        if settings.reminder_lead_minutes != minutes:
            settings.reminder_lead_minutes = minutes
            settings = await db.save_settings(settings, clock.now_utc())
            await sync_plan(db, clock, config)
            await callback.answer("Интервал сохранён")
        else:
            await callback.answer("Этот интервал уже выбран")
        await _show(callback.message, settings, config, edit=True)


@router.message(StateFilter(SettingsDialog.enter_time))
async def on_digest_time_text(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = parse_exact_time(message.text or "")
    if parsed is None:
        await message.answer(
            "Нужно время в виде ЧЧ:ММ, например 09:00.",
            reply_markup=digest_time_keyboard(),
        )
        return
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != SettingsDialog.enter_time.state:
            return
        settings = await db.ensure_settings(config.allowed_user_id, clock.now_utc())
        settings.digest_time = parsed
        stored = await db.save_settings(settings, clock.now_utc())
        await sync_plan(db, clock, config)
        await state.clear()
        await message.answer(
            "Время сводки сохранено.\n" + render_settings(stored, config.timezone.key),
            reply_markup=settings_keyboard(stored),
        )


async def _show(message, settings, config: Config, *, edit: bool) -> None:
    text = render_settings(settings, config.timezone.key)
    markup = settings_keyboard(settings)
    if edit:
        await safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _blocked_by_clarify(callback, state: FSMContext) -> bool:
    current = await state.get_state()
    if current and str(current).startswith(Clarify.__name__):
        await callback.answer(
            "Сначала закончите добавление задачи или нажмите /cancel.",
            show_alert=True,
        )
        return True
    return False
