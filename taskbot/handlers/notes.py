from types import SimpleNamespace

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from taskbot.categories import NO_CATEGORY, category_names, seed_defaults
from taskbot.config import Config
from taskbot.dates import Clock, from_utc
from taskbot.db import Database
from taskbot.formatting import h
from taskbot.handlers.intake import handle_new_tasks
from taskbot.handlers.states import NoteDialog
from taskbot.handlers.view import safe_edit
from taskbot.keyboards import EMPTY_INLINE
from taskbot.locks import UserLocks
from taskbot.notes import (
    FILTER_ALL,
    FILTER_PINNED,
    Note,
    add_note,
    count_linked_tasks,
    delete_note,
    first_line,
    get_note,
    list_notes,
    list_title,
    set_pinned,
    split_parts,
    update_text,
)
from taskbot.texts import STALE

router = Router(name="notes")

NOTES_PAGE_SIZE = 6
NOTE_STALE = "Заметка не найдена или уже удалена."
TASK_TITLE_LIMIT = 120
_DIALOG_STATES = (NoteDialog.add, NoteDialog.edit, NoteDialog.task_title)


# ---------- отображение ----------


async def open_notes(message, state: FSMContext, db: Database, clock: Clock, config: Config) -> None:
    """Вход из меню. Состояние уже очищено, фильтр сбрасывается."""

    await state.update_data(nf=FILTER_ALL)
    await show_notes(message, db, clock, config, state, 0, edit=False)


async def show_notes(
    target,
    db: Database,
    clock: Clock,
    config: Config,
    state: FSMContext,
    page: int,
    *,
    edit: bool,
    notice: str | None = None,
) -> None:
    owner = config.allowed_user_id
    await seed_defaults(db, owner, clock.now_utc())
    data = await state.get_data()
    flt = str(data.get("nf") or FILTER_ALL)
    names = await category_names(db, owner)
    if flt.startswith("c") and flt != "c0" and (not flt[1:].isdigit() or int(flt[1:]) not in names):
        flt = FILTER_ALL
        await state.update_data(nf=flt)
    notes = await list_notes(db, owner, flt)
    pages = max(1, (len(notes) + NOTES_PAGE_SIZE - 1) // NOTES_PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    chunk = notes[page * NOTES_PAGE_SIZE : (page + 1) * NOTES_PAGE_SIZE]
    lines = ["📝 Заметки"]
    if notice:
        lines.insert(0, notice)
    label = _filter_label(flt, names)
    if flt != FILTER_ALL:
        lines.append(f"Фильтр: {h(label)}")
    if not notes:
        lines.append("")
        lines.append("Заметок нет." if flt == FILTER_ALL else "В этом фильтре заметок нет.")
    for note in chunk:
        pin = "📌 " if note.pinned else ""
        tag = f" · 🏷 {h(names[note.category_id])}" if note.category_id in names else ""
        lines.append(f"• {pin}{h(list_title(note))}{tag}")
    if pages > 1:
        lines.append("")
        lines.append(f"Страница {page + 1} из {pages}")
    rows = [
        [
            InlineKeyboardButton(text="➕ Добавить заметку", callback_data="nt:add"),
            InlineKeyboardButton(
                text="Все заметки" if flt == FILTER_PINNED else "📌 Закреплённые",
                callback_data="nt:f:all" if flt == FILTER_PINNED else "nt:f:pin",
            ),
        ],
        [
            InlineKeyboardButton(
                text=_clip(f"🏷 Категория: {label}"),
                callback_data="nt:fc",
            )
        ],
    ]
    for note in chunk:
        pin = "📌 " if note.pinned else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=_clip(f"{pin}{list_title(note)}"),
                    callback_data=f"nt:o:{note.id}:n{page}",
                )
            ]
        )
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"nt:l:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"nt:l:{page + 1}"))
        rows.append(nav)
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "\n".join(lines)
    if edit:
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


def note_keyboard(note: Note, ctx: str) -> InlineKeyboardMarkup:
    source, page = parse_ctx(ctx)
    back = (
        InlineKeyboardButton(text="⬅️ К поиску", callback_data=f"l:sr:{page}")
        if source == "s"
        else InlineKeyboardButton(text="⬅️ К заметкам", callback_data=f"nt:l:{page}")
    )
    pin = (
        InlineKeyboardButton(text="Открепить", callback_data=f"nt:p:{note.id}:0:{ctx}")
        if note.pinned
        else InlineKeyboardButton(text="📌 Закрепить", callback_data=f"nt:p:{note.id}:1:{ctx}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"nt:ed:{note.id}:{ctx}"),
                pin,
            ],
            [
                InlineKeyboardButton(text="🏷 Категория", callback_data=f"cs:n:{note.id}:{ctx}"),
                InlineKeyboardButton(text="📋 Сделать задачей", callback_data=f"nt:t:{note.id}:{ctx}"),
            ],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"nt:d:{note.id}:{ctx}")],
            [back],
        ]
    )


async def render_note_parts(
    db: Database,
    clock: Clock,
    config: Config,
    note: Note,
    notice: str | None = None,
) -> list[str]:
    owner = config.allowed_user_id
    names = await category_names(db, owner)
    linked = await count_linked_tasks(db, owner, note.id)
    updated = from_utc(note.updated_at, clock.tz).strftime("%d.%m.%Y %H:%M")
    chunks = split_parts(note.text)
    total = len(chunks)
    head = [f"📝 <b>Заметка №{note.id}</b>" + (" 📌" if note.pinned else "")]
    if notice:
        head.insert(0, notice)
    category = names.get(note.category_id or -1)
    head.append(f"Категория: {h(category) if category else NO_CATEGORY}")
    head.append(f"Обновлена: {updated}")
    if linked:
        head.append(f"Задач из заметки: {linked}")
    parts: list[str] = []
    for index, chunk in enumerate(chunks):
        if index == 0:
            lines = list(head)
            if total > 1:
                lines.append(f"Часть 1 из {total}")
        else:
            lines = [f"📝 Заметка №{note.id}, часть {index + 1} из {total}"]
        lines.append("")
        lines.append(h(chunk))
        parts.append("\n".join(lines))
    return parts


async def show_note(
    target,
    db: Database,
    clock: Clock,
    config: Config,
    note: Note,
    ctx: str,
    *,
    edit: bool,
    notice: str | None = None,
) -> None:
    """Длинная заметка выводится несколькими сообщениями без потери текста."""

    parts = await render_note_parts(db, clock, config, note, notice)
    markup = note_keyboard(note, ctx)
    if len(parts) == 1:
        if edit:
            await safe_edit(target, parts[0], markup)
        else:
            await target.answer(parts[0], reply_markup=markup)
        return
    if edit:
        await safe_edit(target, parts[0], EMPTY_INLINE)
    else:
        await target.answer(parts[0])
    for part in parts[1:-1]:
        await target.answer(part)
    await target.answer(parts[-1], reply_markup=markup)


# ---------- навигация ----------


@router.callback_query(F.data.startswith("nt:l:"))
async def on_notes_page(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    page = _int_part(callback.data, 2)
    if page is None or page < 0:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        await callback.answer()
        await show_notes(callback.message, db, clock, config, state, page, edit=True)


@router.callback_query(F.data.startswith("nt:f:"))
async def on_notes_filter(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    value = (callback.data or "").split(":", 2)[-1]
    valid = value in {FILTER_ALL, FILTER_PINNED, "c0"} or (value.startswith("c") and value[1:].isdigit())
    if not valid:
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        await state.update_data(nf=value)
        await callback.answer()
        await show_notes(callback.message, db, clock, config, state, 0, edit=True)


@router.callback_query(F.data == "nt:fc")
async def on_notes_filter_pick(
    callback,
    db: Database,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        names = await category_names(db, config.allowed_user_id)
        buttons = [
            InlineKeyboardButton(text="Все", callback_data="nt:f:all"),
            InlineKeyboardButton(text=NO_CATEGORY, callback_data="nt:f:c0"),
        ]
        buttons.extend(
            InlineKeyboardButton(text=_clip(name), callback_data=f"nt:f:c{cid}")
            for cid, name in names.items()
        )
        rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton(text="⬅️ К заметкам", callback_data="nt:l:0")])
        await callback.answer()
        await safe_edit(
            callback.message,
            "Выберите категорию для фильтра заметок.",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("nt:o:"))
async def on_note_open(
    callback,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_id_ctx(callback.data, 2)
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    note_id, ctx = parsed
    async with locks.lock(config.allowed_user_id):
        note = await get_note(db, config.allowed_user_id, note_id)
        if note is None:
            await _missing(callback)
            return
        await callback.answer()
        await show_note(callback.message, db, clock, config, note, ctx, edit=True)


# ---------- добавление и изменение ----------


@router.callback_query(F.data == "nt:add")
async def on_note_add(
    callback,
    state: FSMContext,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        await state.set_data({})
        await state.set_state(NoteDialog.add)
        await callback.answer()
        await callback.message.answer(
            "Отправьте текст заметки одним сообщением. Переносы строк сохранятся, "
            "даты и сроки из текста не извлекаются.",
            reply_markup=_cancel_keyboard(),
        )


@router.callback_query(F.data.startswith("nt:ed:"))
async def on_note_edit(
    callback,
    state: FSMContext,
    db: Database,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_id_ctx(callback.data, 2)
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    note_id, ctx = parsed
    async with locks.lock(config.allowed_user_id):
        note = await get_note(db, config.allowed_user_id, note_id)
        if note is None:
            await _missing(callback)
            return
        await state.set_data({"note_id": note_id, "ctx": ctx})
        await state.set_state(NoteDialog.edit)
        await callback.answer()
        await callback.message.answer(
            f"Отправьте новый текст заметки №{note_id}. Прежний текст будет заменён целиком.",
            reply_markup=_cancel_keyboard(),
        )


@router.callback_query(StateFilter(*_DIALOG_STATES), F.data == "nt:x")
async def on_note_cancel(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        data = await state.get_data()
        await state.clear()
        await callback.answer("Отменено")
        note_id = data.get("note_id")
        note = await get_note(db, config.allowed_user_id, int(note_id)) if note_id else None
        if note is not None:
            await show_note(
                callback.message,
                db,
                clock,
                config,
                note,
                str(data.get("ctx") or "n0"),
                edit=False,
                notice="Отменено. Заметка осталась прежней.",
            )
            return
        await state.update_data(nf=FILTER_ALL)
        await show_notes(callback.message, db, clock, config, state, 0, edit=False, notice="Отменено.")


@router.callback_query(F.data == "nt:x")
async def on_note_cancel_stale(callback) -> None:
    await callback.answer(STALE, show_alert=True)


@router.message(StateFilter(NoteDialog.add), F.text)
async def on_note_text_add(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != NoteDialog.add.state:
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer("Заметка пустая. Отправьте текст или нажмите «Отмена».", reply_markup=_cancel_keyboard())
            return
        note = await add_note(db, config.allowed_user_id, text, clock.now_utc())
        await state.clear()
        await show_note(message, db, clock, config, note, "n0", edit=False, notice="Заметка сохранена.")


@router.message(StateFilter(NoteDialog.edit), F.text)
async def on_note_text_edit(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != NoteDialog.edit.state:
            return
        data = await state.get_data()
        text = (message.text or "").strip()
        if not text:
            await message.answer("Текст пустой. Отправьте новый текст или нажмите «Отмена».", reply_markup=_cancel_keyboard())
            return
        note = await update_text(
            db, config.allowed_user_id, int(data.get("note_id") or 0), text, clock.now_utc()
        )
        await state.clear()
        if note is None:
            await message.answer(NOTE_STALE)
            return
        await show_note(
            message, db, clock, config, note, str(data.get("ctx") or "n0"),
            edit=False, notice="Заметка обновлена.",
        )


@router.message(StateFilter(*_DIALOG_STATES), ~F.text)
async def on_note_other(message, state: FSMContext) -> None:
    await message.answer(
        "Нужен текст сообщением. Отмена — кнопка «Отмена» или /cancel.",
        reply_markup=_cancel_keyboard(),
    )


# ---------- закрепление, удаление ----------


@router.callback_query(F.data.startswith("nt:p:"))
async def on_note_pin(
    callback,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    parts = (callback.data or "").split(":")
    try:
        note_id = int(parts[2])
        value = parts[3]
        ctx = parts[4]
    except (IndexError, ValueError):
        await callback.answer(STALE, show_alert=True)
        return
    if value not in {"0", "1"} or not _valid_ctx(ctx):
        await callback.answer(STALE, show_alert=True)
        return
    async with locks.lock(config.allowed_user_id):
        note = await set_pinned(db, config.allowed_user_id, note_id, value == "1")
        if note is None:
            await _missing(callback)
            return
        await callback.answer("Закреплено" if note.pinned else "Откреплено")
        await show_note(callback.message, db, clock, config, note, ctx, edit=True)


@router.callback_query(F.data.startswith("nt:d:"))
async def on_note_delete_ask(
    callback,
    db: Database,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_id_ctx(callback.data, 2)
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    note_id, ctx = parsed
    async with locks.lock(config.allowed_user_id):
        note = await get_note(db, config.allowed_user_id, note_id)
        if note is None:
            await _missing(callback)
            return
        await callback.answer()
        await callback.message.answer(
            f"Удалить заметку №{note_id}?\n<b>{h(list_title(note))}</b>\n"
            "Задачи, созданные из неё, останутся.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Удалить", callback_data=f"nt:dy:{note_id}:{ctx}")],
                    [InlineKeyboardButton(text="Отмена", callback_data=f"nt:o:{note_id}:{ctx}")],
                ]
            ),
        )


@router.callback_query(F.data.startswith("nt:dy:"))
async def on_note_delete_yes(
    callback,
    db: Database,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_id_ctx(callback.data, 2)
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    note_id, _ctx = parsed
    back = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ К заметкам", callback_data="nt:l:0")]]
    )
    async with locks.lock(config.allowed_user_id):
        deleted = await delete_note(db, config.allowed_user_id, note_id)
        if not deleted:
            await callback.answer("Заметка уже удалена.", show_alert=True)
            await safe_edit(callback.message, "Заметка уже удалена.", back)
            return
        await callback.answer("Заметка удалена")
        await safe_edit(callback.message, "Заметка удалена.", back)


# ---------- заметка -> задача ----------


@router.callback_query(F.data.startswith("nt:t:"))
async def on_note_to_task(
    callback,
    state: FSMContext,
    db: Database,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse_id_ctx(callback.data, 2)
    if parsed is None:
        await callback.answer(STALE, show_alert=True)
        return
    note_id, ctx = parsed
    async with locks.lock(config.allowed_user_id):
        note = await get_note(db, config.allowed_user_id, note_id)
        if note is None:
            await _missing(callback)
            return
        proposal = proposed_title(note.text)
        await state.set_data({"note_id": note_id, "ctx": ctx, "proposal": proposal})
        await state.set_state(NoteDialog.task_title)
        await callback.answer()
        await callback.message.answer(
            f"Название задачи:\n<b>{h(proposal)}</b>\n\n"
            "Подтвердите или отправьте своё название. Дату и время можно написать "
            "в названии, например «завтра в 15:00 …»; если даты нет, я спрошу. "
            "Заметка останется целиком.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Использовать название", callback_data="nt:tu")],
                    [InlineKeyboardButton(text="Отмена", callback_data="nt:x")],
                ]
            ),
        )


@router.callback_query(StateFilter(NoteDialog.task_title), F.data == "nt:tu")
async def on_note_task_use(
    callback,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != NoteDialog.task_title.state:
            await callback.answer(STALE, show_alert=True)
            return
        data = await state.get_data()
        note_id = int(data.get("note_id") or 0)
        title = str(data.get("proposal") or "")
        if await get_note(db, config.allowed_user_id, note_id) is None or not title:
            await state.clear()
            await callback.answer(NOTE_STALE, show_alert=True)
            return
        await callback.answer()
        shim = SimpleNamespace(
            text=title,
            message_id=callback.message.message_id,
            chat=callback.message.chat,
            answer=callback.message.answer,
        )
        await state.clear()
        await handle_new_tasks(shim, state, db, clock, config, note_id=note_id)


@router.callback_query(F.data == "nt:tu")
async def on_note_task_use_stale(callback) -> None:
    await callback.answer(STALE, show_alert=True)


@router.message(StateFilter(NoteDialog.task_title), F.text)
async def on_note_task_text(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != NoteDialog.task_title.state:
            return
        data = await state.get_data()
        note_id = int(data.get("note_id") or 0)
        title = " ".join((message.text or "").split())
        if not title:
            await message.answer("Название пустое. Отправьте текст или нажмите «Отмена».", reply_markup=_cancel_keyboard())
            return
        if await get_note(db, config.allowed_user_id, note_id) is None:
            await state.clear()
            await message.answer(NOTE_STALE)
            return
        shim = SimpleNamespace(
            text=title,
            message_id=message.message_id,
            chat=message.chat,
            answer=message.answer,
        )
        await state.clear()
        await handle_new_tasks(shim, state, db, clock, config, note_id=note_id)


# ---------- вспомогательное ----------


def proposed_title(text: str) -> str:
    """Начало заметки как название. Полный текст остаётся в заметке."""

    line = first_line(text)
    if len(line) > TASK_TITLE_LIMIT:
        line = line[: TASK_TITLE_LIMIT - 1].rstrip() + "…"
    return line


def parse_ctx(ctx: str) -> tuple[str, int]:
    source = ctx[:1] if ctx[:1] in {"n", "s"} else "n"
    try:
        page = int(ctx[1:])
    except ValueError:
        page = 0
    return source, max(page, 0)


def _valid_ctx(ctx: str) -> bool:
    return len(ctx) >= 2 and ctx[0] in {"n", "s"} and ctx[1:].isdigit()


def _parse_id_ctx(data: str | None, start: int) -> tuple[int, str] | None:
    parts = (data or "").split(":")
    try:
        note_id = int(parts[start])
        ctx = parts[start + 1]
    except (IndexError, ValueError):
        return None
    if not _valid_ctx(ctx) or note_id <= 0:
        return None
    return note_id, ctx


def _int_part(data: str | None, index: int) -> int | None:
    try:
        return int((data or "").split(":")[index])
    except (IndexError, ValueError):
        return None


def _filter_label(flt: str, names: dict[int, str]) -> str:
    if flt == FILTER_PINNED:
        return "Закреплённые"
    if flt == "c0":
        return NO_CATEGORY
    if flt.startswith("c") and flt[1:].isdigit():
        return names.get(int(flt[1:]), "все")
    return "все"


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="nt:x")]]
    )


def _clip(text: str, limit: int = 64) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


async def _missing(callback) -> None:
    await callback.answer(NOTE_STALE, show_alert=True)
    if getattr(callback, "message", None) is not None:
        await safe_edit(
            callback.message,
            NOTE_STALE,
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ К заметкам", callback_data="nt:l:0")]]
            ),
        )
