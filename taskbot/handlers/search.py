from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from taskbot.config import Config
from taskbot.dates import Clock
from taskbot.db import Database
from taskbot.formatting import MESSAGE_LIMIT, h
from taskbot.handlers.states import SearchDialog
from taskbot.handlers.view import safe_edit
from taskbot.locks import UserLocks
from taskbot.search import Hit, normalize_query, search
from taskbot.texts import SCOPE_SEARCH, STALE

router = Router(name="search")

SEARCH_PAGE_SIZE = 6
STALE_SEARCH = "Поиск устарел. Нажмите «🔎 Поиск» и введите запрос ещё раз."


async def open_search(message, state: FSMContext) -> None:
    await state.set_data({})
    await state.set_state(SearchDialog.query)
    await message.answer(
        "Что найти? Отправьте часть названия задачи или текста заметки. "
        "Регистр не важен.",
        reply_markup=_cancel_keyboard(),
    )


@router.callback_query(F.data == "sr:new")
async def on_search_new(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        await callback.answer()
        await open_search(callback.message, state)


@router.callback_query(StateFilter(SearchDialog.query), F.data == "sr:x")
async def on_search_cancel(callback, state: FSMContext, config: Config, locks: UserLocks) -> None:
    async with locks.lock(config.allowed_user_id):
        await state.clear()
        await callback.answer("Поиск отменён")
        await callback.message.answer("Поиск отменён.")


@router.callback_query(F.data == "sr:x")
async def on_search_cancel_stale(callback) -> None:
    await callback.answer(STALE, show_alert=True)


@router.message(StateFilter(SearchDialog.query), F.text)
async def on_search_text(
    message,
    state: FSMContext,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    async with locks.lock(config.allowed_user_id):
        if await state.get_state() != SearchDialog.query.state:
            return
        query = normalize_query(message.text or "")
        if not query:
            await message.answer(
                "Запрос пустой. Отправьте текст или нажмите «Отмена».",
                reply_markup=_cancel_keyboard(),
            )
            return
        await state.set_state(None)
        await state.set_data({"sq": query})
        await show_results(message, db, clock, config, state, 0, edit=False)


@router.message(StateFilter(SearchDialog.query), ~F.text)
async def on_search_other(message) -> None:
    await message.answer("Нужен текст запроса.", reply_markup=_cancel_keyboard())


async def show_results(
    target,
    db: Database,
    clock: Clock,
    config: Config,
    state: FSMContext,
    page: int,
    *,
    edit: bool,
) -> None:
    data = await state.get_data()
    query = str(data.get("sq") or "")
    if not query:
        await _send(target, STALE_SEARCH, _new_keyboard(), edit)
        return
    hits = await search(db, config.allowed_user_id, query)
    pages = max(1, (len(hits) + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    chunk = hits[page * SEARCH_PAGE_SIZE : (page + 1) * SEARCH_PAGE_SIZE]
    lines = [f"🔎 Поиск: «{h(query)}»"]
    if not hits:
        lines.append("")
        lines.append("Ничего не найдено. Попробуйте другой запрос.")
    else:
        lines.append(f"Найдено: {len(hits)}")
        lines.append("")
        for hit in chunk:
            lines.append(_hit_line(hit))
    if pages > 1:
        lines.append("")
        lines.append(f"Страница {page + 1} из {pages}")
    text = "\n".join(lines)
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 1] + "…"
    rows = []
    for hit in chunk:
        if hit.kind == "task":
            callback = f"o:{SCOPE_SEARCH}:{page}:{hit.id}"
            label = ("✅ " if hit.status == "done" else "📋 ") + hit.title
        else:
            callback = f"nt:o:{hit.id}:s{page}"
            label = "📝 " + hit.title
        rows.append([InlineKeyboardButton(text=_clip(label), callback_data=callback)])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"l:{SCOPE_SEARCH}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"l:{SCOPE_SEARCH}:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔎 Новый поиск", callback_data="sr:new")])
    await _send(target, text, InlineKeyboardMarkup(inline_keyboard=rows), edit)


def _hit_line(hit: Hit) -> str:
    if hit.kind == "task":
        status = "выполнена" if hit.status == "done" else "открыта"
        return f"📋 Задача ({status}): {h(hit.snippet)}"
    return f"📝 Заметка: {h(hit.snippet)}"


async def _send(target, text: str, markup, edit: bool) -> None:
    if edit:
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


def _new_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔎 Новый поиск", callback_data="sr:new")]]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="sr:x")]]
    )


def _clip(text: str, limit: int = 64) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"
