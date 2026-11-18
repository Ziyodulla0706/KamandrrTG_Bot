from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from taskbot.categories import (
    NO_CATEGORY,
    CategoryError,
    create_category,
    delete_category,
    get_category,
    list_categories,
    rename_category,
    seed_defaults,
    usage,
)
from taskbot.config import Config
from taskbot.dates import Clock
from taskbot.db import Database
from taskbot.formatting import h
from taskbot.handlers.notes import parse_ctx, show_note
from taskbot.handlers.states import CatDialog
from taskbot.handlers.view import CAT_ALL, CAT_NONE, safe_edit, show_card, show_list
from taskbot.locks import UserLocks
from taskbot.notes import get_note, set_note_category
from taskbot.texts import (
    SCOPE_ALL,
    SCOPE_DONE,
    SCOPE_TODAY,
    SCOPE_TOMORROW,
    STALE,
)

router = Router(name="categories")

_LIST_SCOPES = {SCOPE_TODAY, SCOPE_TOMORROW, SCOPE_ALL, SCOPE_DONE}
_TASK_SCOPES = _LIST_SCOPES | {"wk", "sr"}
_CAT_STATES = (CatDialog.create, CatDialog.rename)
GONE = "Категория не найдена или уже удалена."


# ---------- управление ----------


async def show_categories(
    target,
    db: Database,
    clock: Clock,
    config: Config,
    *,
    edit: bool,
    notice: str | None = None,
) -> None:
    owner = config.allowed_user_id
    await seed_defaults(db, owner, clock.now_utc())
    items = await list_categories(db, owner)
    lines = ["🏷 Категории"]
    if notice:
        lines.insert(0, notice)
    if not items:
        lines.append("")
        lines.append("Категорий пока нет.")
    rows = []
    for item in items:
        tasks, notes, recs = await usage(db, owner, item.id)
        lines.append(f"• {h(item.name)} — задач {tasks}, заметок {notes}, повторений {recs}")
        rows.append([InlineKeyboardButton(text=_clip(item.name), callback_data=f"ca:o:{item.id}")])
    rows.append([InlineKeyboardButton(text="➕ Новая категория", callback_data="ca:new")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "\n".join(lines)
    if edit:
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == "ca:l")
async def on_cat_list(
    callback, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        await state.clear()
        await callback.answer()
        await show_categories(callback.message, db, clock, config, edit=True)


@router.callback_query(F.data.startswith("ca:o:"))
async def on_cat_open(
    callback, db: Database, config: Config, locks: UserLocks
) -> None:
    cat_id = _int_at(callback.data, 2)
    if cat_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        item = await get_category(db, config.allowed_user_id, cat_id)
        if item is None:
            await _gone(callback)
            return
        tasks, notes, recs = await usage(db, config.allowed_user_id, item.id)
        await callback.answer()
        await safe_edit(
            callback.message,
            f"🏷 <b>{h(item.name)}</b>\nЗадач: {tasks}, заметок: {notes}, повторений: {recs}",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"ca:rn:{item.id}"),
                        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ca:del:{item.id}"),
                    ],
                    [InlineKeyboardButton(text="⬅️ К категориям", callback_data="ca:l")],
                ]
            ),
        )


@router.callback_query(F.data == "ca:new")
async def on_cat_new(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        await state.set_data({})
        await state.set_state(CatDialog.create)
        await callback.answer()
        await callback.message.answer(
            "Отправьте название новой категории.", reply_markup=_cancel_keyboard()
        )


@router.callback_query(F.data.startswith("ca:rn:"))
async def on_cat_rename(
    callback, state: FSMContext, db: Database, config: Config, locks: UserLocks
) -> None:
    cat_id = _int_at(callback.data, 2)
    if cat_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        item = await get_category(db, config.allowed_user_id, cat_id)
        if item is None:
            await _gone(callback)
            return
        await state.set_data({"cat_id": cat_id})
        await state.set_state(CatDialog.rename)
        await callback.answer()
        await callback.message.answer(
            f"Новое название для «{h(item.name)}»:", reply_markup=_cancel_keyboard()
        )


@router.callback_query(StateFilter(*_CAT_STATES), F.data == "ca:x")
async def on_cat_cancel(
    callback, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        await state.clear()
        await callback.answer("Отменено")
        await show_categories(callback.message, db, clock, config, edit=False, notice="Отменено.")


@router.callback_query(F.data == "ca:x")
async def on_cat_cancel_stale(callback) -> None:
    await callback.answer(STALE, show_alert=True)


@router.message(StateFilter(CatDialog.create), F.text)
async def on_cat_create_text(
    message, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != CatDialog.create.state:
            return
        try:
            item = await create_category(db, config.allowed_user_id, message.text or "", clock.now_utc())
        except CategoryError as exc:
            await message.answer(f"{exc} Отправьте другое название.", reply_markup=_cancel_keyboard())
            return
        await state.clear()
        await show_categories(
            message, db, clock, config, edit=False, notice=f"Категория «{h(item.name)}» создана."
        )


@router.message(StateFilter(CatDialog.rename), F.text)
async def on_cat_rename_text(
    message, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != CatDialog.rename.state:
            return
        data = await state.get_data()
        try:
            item = await rename_category(
                db, config.allowed_user_id, int(data.get("cat_id") or 0), message.text or ""
            )
        except CategoryError as exc:
            await message.answer(f"{exc} Отправьте другое название.", reply_markup=_cancel_keyboard())
            return
        await state.clear()
        if item is None:
            await message.answer(GONE)
            return
        await show_categories(
            message, db, clock, config, edit=False, notice=f"Категория переименована: «{h(item.name)}»."
        )


@router.message(StateFilter(*_CAT_STATES), ~F.text)
async def on_cat_other(message) -> None:
    await message.answer("Нужен текст названия.", reply_markup=_cancel_keyboard())


@router.callback_query(F.data.startswith("ca:del:"))
async def on_cat_delete_ask(callback, db: Database, config: Config, locks: UserLocks) -> None:
    cat_id = _int_at(callback.data, 2)
    if cat_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        item = await get_category(db, config.allowed_user_id, cat_id)
        if item is None:
            await _gone(callback)
            return
        await callback.answer()
        await safe_edit(
            callback.message,
            f"Удалить категорию «{h(item.name)}»?\n"
            "Задачи, заметки и повторения останутся, у них будет «Без категории».",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Удалить", callback_data=f"ca:dy:{item.id}")],
                    [InlineKeyboardButton(text="Отмена", callback_data=f"ca:o:{item.id}")],
                ]
            ),
        )


@router.callback_query(F.data.startswith("ca:dy:"))
async def on_cat_delete_yes(
    callback, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    cat_id = _int_at(callback.data, 2)
    if cat_id is None:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        deleted = await delete_category(db, config.allowed_user_id, cat_id)
        if not deleted:
            await callback.answer("Категория уже удалена.", show_alert=True)
            await show_categories(callback.message, db, clock, config, edit=True)
            return
        await callback.answer("Категория удалена")
        await show_categories(
            callback.message, db, clock, config, edit=True, notice="Категория удалена. Записи сохранены."
        )


# ---------- выбор категории для записи ----------


@router.callback_query(F.data.startswith("cs:"))
async def on_pick_start(
    callback, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    parsed = _parse_target(callback.data, 1)
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    kind, target_id, ctx = parsed
    async with locks.lock(config.allowed_user_id):
        await seed_defaults(db, config.allowed_user_id, clock.now_utc())
        if not await _target_exists(db, config, kind, target_id):
            await _gone_target(callback, kind)
            return
        items = await list_categories(db, config.allowed_user_id)
        buttons = [
            InlineKeyboardButton(text=NO_CATEGORY, callback_data=f"cx:{kind}:{target_id}:{ctx}:0")
        ]
        buttons.extend(
            InlineKeyboardButton(
                text=_clip(item.name), callback_data=f"cx:{kind}:{target_id}:{ctx}:{item.id}"
            )
            for item in items
        )
        rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
        rows.append(
            [InlineKeyboardButton(text="Отмена", callback_data=f"cb:{kind}:{target_id}:{ctx}")]
        )
        await callback.answer()
        await callback.message.answer(
            "Выберите категорию.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )


@router.callback_query(F.data.startswith("cb:"))
async def on_pick_back(
    callback, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    parsed = _parse_target(callback.data, 1)
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    kind, target_id, ctx = parsed
    async with locks.lock(config.allowed_user_id):
        await callback.answer("Отменено")
        await _show_target(callback.message, db, clock, config, kind, target_id, ctx, "Выбор отменён.")


@router.callback_query(F.data.startswith("cx:"))
async def on_pick_apply(
    callback, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    parts = (callback.data or "").split(":")
    parsed = _parse_target(":".join(parts[:4]), 1) if len(parts) == 5 else None
    if parsed is None or not parts[4].isdigit():
        await callback.answer(STALE, show_alert=True)
        return
    kind, target_id, ctx = parsed
    category_id = int(parts[4]) or None
    owner = config.allowed_user_id
    async with locks.lock(owner):
        if category_id is not None and await get_category(db, owner, category_id) is None:
            await callback.answer(GONE, show_alert=True)
            return
        if not await _target_exists(db, config, kind, target_id):
            await _gone_target(callback, kind)
            return
        now = clock.now_utc()
        if kind == "t":
            await db.set_category(owner, target_id, category_id, now)
        elif kind == "n":
            await set_note_category(db, owner, target_id, category_id, now)
        else:
            from taskbot.recurrence import update_series

            await update_series(db, clock, owner, target_id, category_id=category_id)
        await callback.answer("Категория сохранена")
        await _show_target(
            callback.message, db, clock, config, kind, target_id, ctx, "Категория сохранена."
        )


# ---------- фильтр списков задач ----------


@router.callback_query(F.data.startswith("tf:"))
async def on_task_filter(
    callback, state: FSMContext, db: Database, clock: Clock, config: Config, locks: UserLocks
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in _LIST_SCOPES:
        await callback.answer(STALE, show_alert=True)
        return
    scope, value = parts[1], parts[2]
    if value not in {"pick", CAT_ALL, CAT_NONE} and not value.isdigit():
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        if value == "pick":
            items = await list_categories(db, config.allowed_user_id)
            buttons = [
                InlineKeyboardButton(text="Все", callback_data=f"tf:{scope}:{CAT_ALL}"),
                InlineKeyboardButton(text=NO_CATEGORY, callback_data=f"tf:{scope}:{CAT_NONE}"),
            ]
            buttons.extend(
                InlineKeyboardButton(text=_clip(item.name), callback_data=f"tf:{scope}:{item.id}")
                for item in items
            )
            rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
            rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data=f"l:{scope}:0")])
            await callback.answer()
            await safe_edit(
                callback.message,
                "Показать задачи категории:",
                InlineKeyboardMarkup(inline_keyboard=rows),
            )
            return
        await state.update_data(tcat=value)
        await callback.answer()
        await show_list(callback.message, db, clock, config, scope, 0, edit=True, cat=value)


# ---------- вспомогательное ----------


async def _target_exists(db: Database, config: Config, kind: str, target_id: int) -> bool:
    owner = config.allowed_user_id
    if kind == "t":
        return await db.get_task(owner, target_id) is not None
    if kind == "n":
        return await get_note(db, owner, target_id) is not None
    from taskbot.recurrence import get_recurrence

    return await get_recurrence(db, owner, target_id) is not None


async def _show_target(
    message,
    db: Database,
    clock: Clock,
    config: Config,
    kind: str,
    target_id: int,
    ctx: str,
    notice: str,
) -> None:
    owner = config.allowed_user_id
    if kind == "t":
        task = await db.get_task(owner, target_id)
        if task is None:
            await message.answer("Задача не найдена или уже удалена.")
            return
        scope, page = _split_task_ctx(ctx)
        await show_card(message, task, clock, scope, page, edit=False, notice=notice, db=db)
    elif kind == "n":
        note = await get_note(db, owner, target_id)
        if note is None:
            await message.answer("Заметка не найдена или уже удалена.")
            return
        await show_note(message, db, clock, config, note, ctx, edit=False, notice=notice)
    else:
        from taskbot.handlers.recur import show_series

        await show_series(message, db, clock, config, target_id, edit=False, notice=notice)


async def _gone_target(callback, kind: str) -> None:
    text = {
        "t": "Задача не найдена или уже удалена.",
        "n": "Заметка не найдена или уже удалена.",
    }.get(kind, "Повторение не найдено или уже удалено.")
    await callback.answer(text, show_alert=True)


def _parse_target(data: str | None, start: int) -> tuple[str, int, str] | None:
    parts = (data or "").split(":")
    try:
        kind = parts[start]
        target_id = int(parts[start + 1])
        ctx = parts[start + 2]
    except (IndexError, ValueError):
        return None
    if kind not in {"t", "n", "r"} or target_id <= 0:
        return None
    if kind == "t":
        scope, page = ctx[:2], ctx[2:]
        if scope not in _TASK_SCOPES or not page.isdigit():
            return None
    elif kind == "n":
        if len(ctx) < 2 or ctx[0] not in {"n", "s"} or not ctx[1:].isdigit():
            return None
    return kind, target_id, ctx


def _split_task_ctx(ctx: str) -> tuple[str, int]:
    return ctx[:2], int(ctx[2:] or 0)


def _int_at(data: str | None, index: int) -> int | None:
    try:
        value = int((data or "").split(":")[index])
    except (IndexError, ValueError):
        return None
    return value if value > 0 else None


async def _gone(callback) -> None:
    await callback.answer(GONE, show_alert=True)


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="ca:x")]]
    )


def _clip(text: str, limit: int = 64) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


__all__ = ["router", "show_categories", "parse_ctx"]
