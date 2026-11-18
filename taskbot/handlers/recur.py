from datetime import date, time, timedelta

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from taskbot.categories import NO_CATEGORY, category_names, list_categories, seed_defaults
from taskbot.config import Config
from taskbot.dates import Clock
from taskbot.db import Database
from taskbot.formatting import h
from taskbot.handlers.states import RecurDialog, RecurEdit
from taskbot.handlers.view import safe_edit
from taskbot.locks import UserLocks
from taskbot.parsing import INVALID_DATE, PAST, parse_exact_date, parse_exact_time
from taskbot.recurrence import (
    MAX_TITLE,
    WEEKDAY_SHORT,
    Recurrence,
    create_recurrence,
    delete_series,
    describe_schedule,
    first_date,
    generate_for,
    get_recurrence,
    list_recurrences,
    month_rule_note,
    plan_template_change,
    set_active,
    update_template,
    upcoming_dates,
)
from taskbot.texts import STALE

router = Router(name="recur")

PAGE_SIZE = 8
GONE = "Повторение не найдено или уже удалено."
_DIALOG = (
    RecurDialog.title,
    RecurDialog.kind,
    RecurDialog.weekdays,
    RecurDialog.month_day,
    RecurDialog.start,
    RecurDialog.start_text,
    RecurDialog.time,
    RecurDialog.category,
    RecurDialog.confirm,
)
_EDIT = (
    RecurEdit.title,
    RecurEdit.kind,
    RecurEdit.weekdays,
    RecurEdit.month_day,
    RecurEdit.time,
    RecurEdit.confirm,
)
_ALL_STATES = _DIALOG + _EDIT


# ---------- раздел «Повторы» ----------


async def show_series_list(
    target,
    db: Database,
    clock: Clock,
    config: Config,
    page: int = 0,
    *,
    edit: bool,
    notice: str | None = None,
) -> None:
    items = await list_recurrences(db, config.allowed_user_id)
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    lines = ["🔁 Повторяющиеся задачи"]
    if notice:
        lines.insert(0, notice)
    if not items:
        lines.append("")
        lines.append("Повторений пока нет.")
    rows = [[InlineKeyboardButton(text="➕ Новое повторение", callback_data="rc:new")]]
    for rec in chunk:
        mark = "" if rec.active else "⏸ "
        lines.append(f"• {mark}{h(rec.title)} — {h(describe_schedule(rec))}")
        rows.append(
            [InlineKeyboardButton(text=_clip(f"{mark}{rec.title}"), callback_data=f"rc:o:{rec.id}")]
        )
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"rc:l:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"rc:l:{page + 1}"))
        rows.append(nav)
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "\n".join(lines)
    if edit:
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("rc:l:"))
async def on_list(callback, db: Database, clock: Clock, config: Config, locks: UserLocks) -> None:
    page = _int_at(callback.data, 2, allow_zero=True)
    if page is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        await callback.answer()
        await show_series_list(callback.message, db, clock, config, page, edit=True)


async def show_series(
    target,
    db: Database,
    clock: Clock,
    config: Config,
    rec_id: int,
    *,
    edit: bool,
    notice: str | None = None,
) -> None:
    owner = config.allowed_user_id
    rec = await get_recurrence(db, owner, rec_id)
    if rec is None:
        await target.answer(GONE)
        return
    names = await category_names(db, owner)
    upcoming = await upcoming_dates(db, rec, clock, 3)
    lines = []
    if notice:
        lines.append(notice)
    lines.append(f"🔁 <b>{h(rec.title)}</b>")
    lines.append(f"Расписание: {h(describe_schedule(rec))}")
    lines.append("Статус: " + ("активно" if rec.active else "приостановлено"))
    lines.append(f"Начало: {rec.start_date.strftime('%d.%m.%Y')}")
    lines.append(f"Категория: {h(names.get(rec.category_id or -1, NO_CATEGORY))}")
    if rec.active and upcoming:
        lines.append("Ближайшие: " + ", ".join(day.strftime("%d.%m") for day in upcoming))
    note = month_rule_note(rec.month_day)
    if note:
        lines.append(note)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить шаблон", callback_data=f"rc:ed:{rec.id}")],
            [
                InlineKeyboardButton(text="🏷 Категория", callback_data=f"cs:r:{rec.id}:x"),
                (
                    InlineKeyboardButton(text="⏸ Приостановить", callback_data=f"rc:ps:{rec.id}")
                    if rec.active
                    else InlineKeyboardButton(text="▶️ Возобновить", callback_data=f"rc:rs:{rec.id}")
                ),
            ],
            [InlineKeyboardButton(text="🗑 Удалить серию", callback_data=f"rc:del:{rec.id}")],
            [InlineKeyboardButton(text="⬅️ К повторениям", callback_data="rc:l:0")],
        ]
    )
    text = "\n".join(lines)
    if edit:
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("rc:o:"))
async def on_open(
    callback, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    rec_id = _int_at(callback.data, 2)
    if rec_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        if await get_recurrence(db, config.allowed_user_id, rec_id) is None:
            await callback.answer(GONE, show_alert=True)
            return
        await callback.answer()
        await show_series(callback.message, db, clock, config, rec_id, edit=False)


# ---------- изменение серии ----------


@router.callback_query(F.data.startswith("rc:ps:"))
async def on_pause_ask(callback, db: Database, config: Config, locks: UserLocks) -> None:
    rec_id = _int_at(callback.data, 2)
    if rec_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        rec = await get_recurrence(db, config.allowed_user_id, rec_id)
        if rec is None:
            await callback.answer(GONE, show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Приостановить «{h(rec.title)}»?\n"
            "Будущие открытые задачи серии будут удалены вместе с напоминаниями. "
            "Выполненные, прошлые и отдельно перенесённые останутся.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Приостановить", callback_data=f"rc:psy:{rec.id}")],
                    [InlineKeyboardButton(text="Отмена", callback_data=f"rc:o:{rec.id}")],
                ]
            ),
        )


@router.callback_query(F.data.startswith("rc:psy:"))
async def on_pause_yes(
    callback, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    await _set_active(callback, db, clock, config, locks, False)


@router.callback_query(F.data.startswith("rc:rs:"))
async def on_resume(callback, db: Database, clock: Clock, config: Config, locks: UserLocks) -> None:
    await _set_active(callback, db, clock, config, locks, True)


async def _set_active(callback, db, clock, config, locks, active: bool) -> None:
    rec_id = _int_at(callback.data, 2)
    if rec_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        rec = await set_active(db, clock, config.allowed_user_id, rec_id, active)
        if rec is None:
            await callback.answer(GONE, show_alert=True)
            return
        await callback.answer("Возобновлено" if active else "Приостановлено")
        await show_series(
            callback.message, db, clock, config, rec_id, edit=False,
            notice="Серия возобновлена." if active else "Серия приостановлена.",
        )


@router.callback_query(F.data.startswith("rc:del:"))
async def on_delete_ask(callback, db: Database, config: Config, locks: UserLocks) -> None:
    rec_id = _int_at(callback.data, 2)
    if rec_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        rec = await get_recurrence(db, config.allowed_user_id, rec_id)
        if rec is None:
            await callback.answer(GONE, show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Удалить всю серию «{h(rec.title)}»?\n"
            "Будущие открытые задачи будут удалены вместе с напоминаниями. "
            "Выполненные, прошлые и отдельно перенесённые задачи останутся как обычные.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Удалить серию", callback_data=f"rc:dy:{rec.id}")],
                    [InlineKeyboardButton(text="Отмена", callback_data=f"rc:o:{rec.id}")],
                ]
            ),
        )


@router.callback_query(F.data.startswith("rc:dy:"))
async def on_delete_yes(
    callback, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    rec_id = _int_at(callback.data, 2)
    if rec_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        deleted = await delete_series(db, clock, config.allowed_user_id, rec_id)
        if not deleted:
            await callback.answer("Серия уже удалена.", show_alert=True)
            return
        await callback.answer("Серия удалена")
        await show_series_list(
            callback.message, db, clock, config, 0, edit=False, notice="Серия удалена."
        )


@router.callback_query(F.data.startswith("rc:ed:"))
async def on_edit_start(
    callback, state: FSMContext, db: Database, config: Config, locks: UserLocks
) -> None:
    rec_id = _int_at(callback.data, 2)
    if rec_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        rec = await get_recurrence(db, config.allowed_user_id, rec_id)
        if rec is None:
            await callback.answer(GONE, show_alert=True)
            return
        await state.set_data(
            {
                "rec_id": rec_id,
                "title": rec.title,
                "kind": rec.kind,
                "wd": list(rec.weekdays),
                "md": rec.month_day,
                "time": rec.time_local.strftime("%H:%M") if rec.time_local else None,
            }
        )
        await state.set_state(RecurEdit.title)
        await callback.answer()
        await callback.message.answer(
            f"Изменение серии. Сейчас: <b>{h(rec.title)}</b>, {h(describe_schedule(rec))}.\n"
            "Отправьте новое название или оставьте прежнее.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Оставить название", callback_data="rc:et")],
                    [InlineKeyboardButton(text="Отмена", callback_data="rc:x")],
                ]
            ),
        )


async def _edit_ask_kind(target, state: FSMContext) -> None:
    await state.set_state(RecurEdit.kind)
    await target.answer(
        "Как повторять?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Каждый день", callback_data="rc:ek:daily"),
                    InlineKeyboardButton(text="По будням", callback_data="rc:ek:weekdays"),
                ],
                [
                    InlineKeyboardButton(text="Каждую неделю", callback_data="rc:ek:weekly"),
                    InlineKeyboardButton(text="Каждый месяц", callback_data="rc:ek:monthly"),
                ],
                [InlineKeyboardButton(text="Оставить расписание", callback_data="rc:ek:keep")],
                [InlineKeyboardButton(text="Отмена", callback_data="rc:x")],
            ]
        ),
    )


@router.callback_query(StateFilter(RecurEdit.title), F.data == "rc:et")
async def on_edit_title_keep(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurEdit.title.state:
            await callback.answer(STALE, show_alert=True)
            return
        await callback.answer()
        await _edit_ask_kind(callback.message, state)


@router.message(StateFilter(RecurEdit.title), F.text)
async def on_edit_title_text(message, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurEdit.title.state:
            return
        title = " ".join((message.text or "").split())
        if not title or len(title) > MAX_TITLE:
            await message.answer(
                f"Название должно быть от 1 до {MAX_TITLE} символов.", reply_markup=_cancel_keyboard()
            )
            return
        await state.update_data(title=title)
        await _edit_ask_kind(message, state)


@router.callback_query(StateFilter(RecurEdit.kind), F.data.startswith("rc:ek:"))
async def on_edit_kind(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    kind = (callback.data or "").split(":")[-1]
    if kind not in {"daily", "weekdays", "weekly", "monthly", "keep"}:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurEdit.kind.state:
            await callback.answer(STALE, show_alert=True)
            return
        await callback.answer()
        data = await state.get_data()
        if kind == "keep":
            await _edit_ask_time(callback.message, state, data)
            return
        await state.update_data(kind=kind)
        if kind == "weekly":
            chosen = sorted(int(item) for item in data.get("wd") or [])
            await state.set_state(RecurEdit.weekdays)
            await callback.message.answer("Выберите дни недели.", reply_markup=_weekday_keyboard(chosen))
        elif kind == "monthly":
            await state.set_state(RecurEdit.month_day)
            await callback.message.answer(
                "Какого числа повторять? Отправьте число от 1 до 31.\n"
                "Если в месяце нет такого числа (29, 30 или 31), задача будет в последний день месяца.",
                reply_markup=_cancel_keyboard(),
            )
        else:
            await _edit_ask_time(callback.message, state, data)


@router.callback_query(StateFilter(RecurEdit.weekdays), F.data.startswith("rc:w:"))
async def on_edit_weekday_toggle(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    index = _int_at(callback.data, 2, allow_zero=True)
    if index is None or index not in range(7):
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurEdit.weekdays.state:
            await callback.answer(STALE, show_alert=True)
            return
        data = await state.get_data()
        chosen = set(int(item) for item in data.get("wd") or [])
        chosen.symmetric_difference_update({index})
        await state.update_data(wd=sorted(chosen))
        await callback.answer()
        await safe_edit(callback.message, "Выберите дни недели.", _weekday_keyboard(sorted(chosen)))


@router.callback_query(StateFilter(RecurEdit.weekdays), F.data == "rc:wd")
async def on_edit_weekdays_done(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurEdit.weekdays.state:
            await callback.answer(STALE, show_alert=True)
            return
        data = await state.get_data()
        if not data.get("wd"):
            await callback.answer("Выберите хотя бы один день.", show_alert=True)
            return
        await callback.answer()
        await _edit_ask_time(callback.message, state, data)


@router.message(StateFilter(RecurEdit.month_day), F.text)
async def on_edit_month_day(message, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurEdit.month_day.state:
            return
        raw = (message.text or "").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 31:
            await message.answer("Нужно число от 1 до 31.", reply_markup=_cancel_keyboard())
            return
        await state.update_data(md=int(raw))
        note = month_rule_note(int(raw))
        if note:
            await message.answer(note)
        await _edit_ask_time(message, state, await state.get_data())


async def _edit_ask_time(target, state: FSMContext, data: dict) -> None:
    await state.set_state(RecurEdit.time)
    current = data.get("time")
    rows = [[InlineKeyboardButton(text="Без времени", callback_data="rc:en")]]
    if current:
        rows.insert(0, [InlineKeyboardButton(text=f"Оставить {current}", callback_data="rc:ep")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="rc:x")])
    await target.answer(
        "Во сколько? Отправьте время ЧЧ:ММ или выберите вариант.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(StateFilter(RecurEdit.time), F.data.in_({"rc:en", "rc:ep"}))
async def on_edit_time_choice(
    callback, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurEdit.time.state:
            await callback.answer(STALE, show_alert=True)
            return
        if callback.data == "rc:en":
            await state.update_data(time=None)
        await callback.answer()
        await _edit_confirm(callback.message, state, db, clock, config)


@router.message(StateFilter(RecurEdit.time), F.text)
async def on_edit_time_text(
    message, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurEdit.time.state:
            return
        parsed = parse_exact_time(message.text or "")
        if parsed is None:
            await message.answer("Нужно время в виде ЧЧ:ММ, например 09:30.", reply_markup=_cancel_keyboard())
            return
        await state.update_data(time=parsed.strftime("%H:%M"))
        await _edit_confirm(message, state, db, clock, config)


def _edit_values(data: dict) -> dict:
    raw_time = data.get("time")
    return {
        "title": str(data.get("title") or ""),
        "kind": str(data.get("kind") or "daily"),
        "weekdays": tuple(int(item) for item in data.get("wd") or []),
        "month_day": int(data["md"]) if data.get("md") else None,
        "time_local": time(int(raw_time[:2]), int(raw_time[3:5])) if raw_time else None,
    }


async def _edit_confirm(target, state: FSMContext, db: Database, clock: Clock, config: Config) -> None:
    data = await state.get_data()
    rec = await get_recurrence(db, config.allowed_user_id, int(data.get("rec_id") or 0))
    if rec is None:
        await state.clear()
        await target.answer(GONE)
        return
    values = _edit_values(data)
    try:
        plan = await plan_template_change(db, clock, rec, **values)
    except ValueError as exc:
        await state.clear()
        await target.answer(str(exc))
        return
    draft = Recurrence(
        id=rec.id, owner_id=rec.owner_id, title=values["title"], kind=values["kind"],
        weekdays=values["weekdays"], month_day=values["month_day"], start_date=rec.start_date,
        time_local=values["time_local"], category_id=rec.category_id, active=rec.active,
        created_at=rec.created_at, updated_at=rec.updated_at,
    )
    lines = [
        "Проверьте изменения серии:",
        f"<b>{h(draft.title)}</b>",
        f"Расписание: {h(describe_schedule(draft))}",
        "",
        "Что изменится:",
        f"• обновятся будущие открытые задачи: {plan.update};",
        f"• будут удалены (не подходят под новое расписание): {plan.remove};",
        f"• будут созданы на ближайшие 30 дней: {plan.create}.",
        f"Не изменятся: выполненные, прошлые и перенесённые вручную задачи ({plan.kept}).",
        "Категория и связи с заметками сохранятся. Напоминания пересчитаются.",
    ]
    note = month_rule_note(draft.month_day)
    if note:
        lines.append(note)
    if not rec.active:
        lines.append("Серия приостановлена: новые задачи появятся после возобновления.")
    await state.set_state(RecurEdit.confirm)
    await target.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Сохранить", callback_data="rc:es")],
                [InlineKeyboardButton(text="Отмена", callback_data="rc:x")],
            ]
        ),
    )


@router.callback_query(StateFilter(RecurEdit.confirm), F.data == "rc:es")
async def on_edit_save(
    callback, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurEdit.confirm.state:
            await callback.answer(STALE, show_alert=True)
            return
        data = await state.get_data()
        rec_id = int(data.get("rec_id") or 0)
        await state.clear()
        try:
            rec = await update_template(db, clock, config.allowed_user_id, rec_id, **_edit_values(data))
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if rec is None:
            await callback.answer(GONE, show_alert=True)
            return
        await callback.answer("Серия изменена")
        await show_series(callback.message, db, clock, config, rec_id, edit=False, notice="Серия изменена.")


@router.callback_query(F.data == "rc:es")
async def on_edit_save_stale(callback) -> None:
    await callback.answer(STALE, show_alert=True)


# ---------- создание ----------


@router.callback_query(F.data == "rc:new")
async def on_new(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        await state.set_data({})
        await state.set_state(RecurDialog.title)
        await callback.answer()
        await callback.message.answer(
            "Название повторяющейся задачи?", reply_markup=_cancel_keyboard()
        )


@router.callback_query(StateFilter(*_ALL_STATES), F.data == "rc:x")
async def on_cancel(
    callback, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        data = await state.get_data()
        editing = await state.get_state() in {item.state for item in _EDIT}
        await state.clear()
        await callback.answer("Отменено")
        if editing and data.get("rec_id"):
            await show_series(
                callback.message, db, clock, config, int(data["rec_id"]), edit=False,
                notice="Изменение отменено. Серия осталась прежней.",
            )
            return
        await show_series_list(callback.message, db, clock, config, 0, edit=False, notice="Отменено.")


@router.callback_query(F.data == "rc:x")
async def on_cancel_stale(callback) -> None:
    await callback.answer(STALE, show_alert=True)


@router.message(StateFilter(RecurDialog.title), F.text)
async def on_title_text(message, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurDialog.title.state:
            return
        title = " ".join((message.text or "").split())
        if not title or len(title) > MAX_TITLE:
            await message.answer(
                f"Название должно быть от 1 до {MAX_TITLE} символов.", reply_markup=_cancel_keyboard()
            )
            return
        await state.update_data(title=title)
        await state.set_state(RecurDialog.kind)
        await message.answer(
            f"<b>{h(title)}</b>\nКак повторять?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Каждый день", callback_data="rc:k:daily"),
                        InlineKeyboardButton(text="По будням", callback_data="rc:k:weekdays"),
                    ],
                    [
                        InlineKeyboardButton(text="Каждую неделю", callback_data="rc:k:weekly"),
                        InlineKeyboardButton(text="Каждый месяц", callback_data="rc:k:monthly"),
                    ],
                    [InlineKeyboardButton(text="Отмена", callback_data="rc:x")],
                ]
            ),
        )


@router.callback_query(StateFilter(RecurDialog.kind), F.data.startswith("rc:k:"))
async def on_kind(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    kind = (callback.data or "").split(":")[-1]
    if kind not in {"daily", "weekdays", "weekly", "monthly"}:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurDialog.kind.state:
            await callback.answer(STALE, show_alert=True)
            return
        await state.update_data(kind=kind, wd=[], md=None)
        await callback.answer()
        if kind == "weekly":
            await state.set_state(RecurDialog.weekdays)
            await callback.message.answer("Выберите дни недели.", reply_markup=_weekday_keyboard([]))
        elif kind == "monthly":
            await state.set_state(RecurDialog.month_day)
            await callback.message.answer(
                "Какого числа повторять? Отправьте число от 1 до 31.\n"
                "Если в месяце нет такого числа (29, 30 или 31), задача будет в последний день месяца.",
                reply_markup=_cancel_keyboard(),
            )
        else:
            await _ask_start(callback.message, state)


@router.callback_query(StateFilter(RecurDialog.weekdays), F.data.startswith("rc:w:"))
async def on_weekday_toggle(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    index = _int_at(callback.data, 2, allow_zero=True)
    if index is None or index not in range(7):
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurDialog.weekdays.state:
            await callback.answer(STALE, show_alert=True)
            return
        data = await state.get_data()
        chosen = set(int(item) for item in data.get("wd") or [])
        chosen.symmetric_difference_update({index})
        await state.update_data(wd=sorted(chosen))
        await callback.answer()
        await safe_edit(callback.message, "Выберите дни недели.", _weekday_keyboard(sorted(chosen)))


@router.callback_query(StateFilter(RecurDialog.weekdays), F.data == "rc:wd")
async def on_weekdays_done(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurDialog.weekdays.state:
            await callback.answer(STALE, show_alert=True)
            return
        data = await state.get_data()
        if not data.get("wd"):
            await callback.answer("Выберите хотя бы один день.", show_alert=True)
            return
        await callback.answer()
        await _ask_start(callback.message, state)


@router.message(StateFilter(RecurDialog.month_day), F.text)
async def on_month_day_text(message, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurDialog.month_day.state:
            return
        raw = (message.text or "").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 31:
            await message.answer("Нужно число от 1 до 31.", reply_markup=_cancel_keyboard())
            return
        day = int(raw)
        await state.update_data(md=day)
        note = month_rule_note(day)
        if note:
            await message.answer(note)
        await _ask_start(message, state)


async def _ask_start(target, state: FSMContext) -> None:
    await state.set_state(RecurDialog.start)
    await target.answer(
        "С какой даты начать?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Сегодня", callback_data="rc:s:today"),
                    InlineKeyboardButton(text="Завтра", callback_data="rc:s:tomorrow"),
                ],
                [
                    InlineKeyboardButton(text="Другая дата", callback_data="rc:s:other"),
                    InlineKeyboardButton(text="Отмена", callback_data="rc:x"),
                ],
            ]
        ),
    )


@router.callback_query(StateFilter(RecurDialog.start), F.data.startswith("rc:s:"))
async def on_start_choice(
    callback, state: FSMContext, clock: Clock, config: Config, locks: UserLocks
) -> None:
    choice = (callback.data or "").split(":")[-1]
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurDialog.start.state:
            await callback.answer(STALE, show_alert=True)
            return
        await callback.answer()
        if choice == "other":
            await state.set_state(RecurDialog.start_text)
            await callback.message.answer(
                "Отправьте дату: 20.11.2026, 20.11, сегодня или завтра.",
                reply_markup=_cancel_keyboard(),
            )
            return
        if choice not in {"today", "tomorrow"}:
            await callback.message.answer(STALE)
            return
        day = clock.today() + timedelta(days=1 if choice == "tomorrow" else 0)
        await state.update_data(start=day.isoformat())
        await _ask_time(callback.message, state)


@router.message(StateFilter(RecurDialog.start, RecurDialog.start_text), F.text)
async def on_start_text(
    message, state: FSMContext, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() not in {RecurDialog.start.state, RecurDialog.start_text.state}:
            return
        parsed = parse_exact_date(message.text or "", today=clock.today())
        if parsed.kind == INVALID_DATE or parsed.task_date is None:
            await message.answer(
                parsed.message or "Не понял дату. Пример: 20.11.2026.",
                reply_markup=_cancel_keyboard(),
            )
            return
        if parsed.kind == PAST or parsed.task_date < clock.today():
            await message.answer(
                "Начальная дата уже прошла. Укажите сегодняшнюю или будущую дату.",
                reply_markup=_cancel_keyboard(),
            )
            return
        await state.update_data(start=parsed.task_date.isoformat())
        await _ask_time(message, state)


async def _ask_time(target, state: FSMContext) -> None:
    await state.set_state(RecurDialog.time)
    await target.answer(
        "Во сколько? Отправьте время ЧЧ:ММ или выберите «Без времени».",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Без времени", callback_data="rc:t:none")],
                [InlineKeyboardButton(text="Отмена", callback_data="rc:x")],
            ]
        ),
    )


@router.callback_query(StateFilter(RecurDialog.time), F.data == "rc:t:none")
async def on_time_choice(
    callback, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurDialog.time.state:
            await callback.answer(STALE, show_alert=True)
            return
        await state.update_data(time=None)
        await callback.answer()
        await _ask_category(callback.message, state, db, clock, config)


@router.message(StateFilter(RecurDialog.time), F.text)
async def on_time_text(
    message, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurDialog.time.state:
            return
        parsed = parse_exact_time(message.text or "")
        if parsed is None:
            await message.answer("Нужно время в виде ЧЧ:ММ, например 09:30.", reply_markup=_cancel_keyboard())
            return
        await state.update_data(time=parsed.strftime("%H:%M"))
        await _ask_category(message, state, db, clock, config)


async def _ask_category(target, state: FSMContext, db: Database, clock: Clock, config: Config) -> None:
    await seed_defaults(db, config.allowed_user_id, clock.now_utc())
    items = await list_categories(db, config.allowed_user_id)
    buttons = [InlineKeyboardButton(text=NO_CATEGORY, callback_data="rc:c:0")]
    buttons.extend(
        InlineKeyboardButton(text=_clip(item.name), callback_data=f"rc:c:{item.id}") for item in items
    )
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="rc:x")])
    await state.set_state(RecurDialog.category)
    await target.answer("Категория?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(StateFilter(RecurDialog.category), F.data.startswith("rc:c:"))
async def on_category_choice(
    callback, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    value = _int_at(callback.data, 2, allow_zero=True)
    if value is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurDialog.category.state:
            await callback.answer(STALE, show_alert=True)
            return
        names = await category_names(db, config.allowed_user_id)
        if value and value not in names:
            await callback.answer("Категория не найдена. Выберите другую.", show_alert=True)
            return
        await state.update_data(cat=value or None)
        await state.set_state(RecurDialog.confirm)
        data = await state.get_data()
        rec = _draft(data, config.allowed_user_id, clock)
        await callback.answer()
        await callback.message.answer(
            _summary(rec, names, clock),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Сохранить", callback_data="rc:ok")],
                    [InlineKeyboardButton(text="Отмена", callback_data="rc:x")],
                ]
            ),
        )


@router.callback_query(StateFilter(RecurDialog.confirm), F.data == "rc:ok")
async def on_confirm(
    callback, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != RecurDialog.confirm.state:
            await callback.answer(STALE, show_alert=True)
            return
        data = await state.get_data()
        draft = _draft(data, config.allowed_user_id, clock)
        await state.clear()
        rec = await create_recurrence(
            db,
            config.allowed_user_id,
            title=draft.title,
            kind=draft.kind,
            weekdays=draft.weekdays,
            month_day=draft.month_day,
            start_date=draft.start_date,
            time_local=draft.time_local,
            category_id=draft.category_id,
            now=clock.now_utc(),
        )
        created = await generate_for(db, clock, config.allowed_user_id, rec.id)
        await callback.answer("Повторение сохранено")
        await show_series(
            callback.message, db, clock, config, rec.id, edit=False,
            notice=f"Повторение сохранено. Создано задач на ближайшие 30 дней: {created}.",
        )


@router.callback_query(F.data == "rc:ok")
async def on_confirm_stale(callback) -> None:
    await callback.answer(STALE, show_alert=True)


@router.message(StateFilter(*_ALL_STATES))
async def on_dialog_other(message) -> None:
    await message.answer(
        "Сейчас нужен ответ кнопкой или текстом в нужном виде. Отмена — кнопка «Отмена» или /cancel.",
        reply_markup=_cancel_keyboard(),
    )


# ---------- вспомогательное ----------


def _draft(data: dict, owner_id: int, clock: Clock) -> Recurrence:
    raw_time = data.get("time")
    now = clock.now_utc()
    return Recurrence(
        id=0,
        owner_id=owner_id,
        title=str(data.get("title") or ""),
        kind=str(data.get("kind") or "daily"),
        weekdays=tuple(int(item) for item in data.get("wd") or []),
        month_day=int(data["md"]) if data.get("md") else None,
        start_date=date.fromisoformat(str(data.get("start") or clock.today().isoformat())),
        time_local=time(int(raw_time[:2]), int(raw_time[3:5])) if raw_time else None,
        category_id=int(data["cat"]) if data.get("cat") else None,
        active=True,
        created_at=now,
        updated_at=now,
    )


def _summary(rec: Recurrence, names: dict[int, str], clock: Clock) -> str:
    first = first_date(rec, clock.today())
    lines = [
        "Проверьте повторение:",
        f"<b>{h(rec.title)}</b>",
        f"Расписание: {h(describe_schedule(rec))}",
        f"Первая дата: {first.strftime('%d.%m.%Y') if first else 'нет'}",
        f"Категория: {h(names.get(rec.category_id or -1, NO_CATEGORY))}",
        f"Задачи появятся на ближайшие 30 дней и будут добавляться дальше. Часовой пояс: {clock.tz.key}.",
    ]
    note = month_rule_note(rec.month_day)
    if note:
        lines.append(note)
    return "\n".join(lines)


def _weekday_keyboard(chosen: list[int]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=("✅ " if index in chosen else "") + name,
            callback_data=f"rc:w:{index}",
        )
        for index, name in enumerate(WEEKDAY_SHORT)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            buttons[:4],
            buttons[4:],
            [InlineKeyboardButton(text="Готово", callback_data="rc:wd")],
            [InlineKeyboardButton(text="Отмена", callback_data="rc:x")],
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="rc:x")]]
    )


def _int_at(data: str | None, index: int, *, allow_zero: bool = False) -> int | None:
    try:
        value = int((data or "").split(":")[index])
    except (IndexError, ValueError):
        return None
    if value < 0 or (value == 0 and not allow_zero):
        return None
    return value


def _clip(text: str, limit: int = 64) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"
