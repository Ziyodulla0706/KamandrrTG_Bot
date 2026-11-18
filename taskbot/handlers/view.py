from aiogram.exceptions import TelegramBadRequest

from taskbot.categories import NO_CATEGORY, category_names
from taskbot.config import Config
from taskbot.dates import Clock
from taskbot.db import Database, Task
from taskbot.formatting import h, render_card, render_list
from taskbot.keyboards import EMPTY_INLINE, card_keyboard, list_keyboard, main_menu
from taskbot.texts import SCOPE_DONE, SCOPE_WEEK
from taskbot.week import render_week

CAT_ALL = "all"
CAT_NONE = "0"


async def safe_edit(message, text: str, reply_markup) -> None:
    edit = getattr(message, "edit_text", None)
    if edit is None:
        answer = getattr(message, "answer", None)
        if answer is not None:
            await answer(text, reply_markup=reply_markup)
        return
    try:
        await edit(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        answer = getattr(message, "answer", None)
        if answer is not None:
            await answer(text, reply_markup=reply_markup)


def filter_label(names: dict[int, str], cat: str) -> str:
    if cat == CAT_ALL:
        return "все"
    if cat == CAT_NONE:
        return NO_CATEGORY
    return names.get(int(cat), "все") if cat.isdigit() else "все"


async def show_list(
    target,
    db: Database,
    clock: Clock,
    config: Config,
    scope: str,
    page: int,
    *,
    edit: bool,
    cat: str = CAT_ALL,
) -> None:
    if scope == SCOPE_WEEK:
        await show_week(target, db, clock, config, page, edit=edit)
        return
    status = "done" if scope == SCOPE_DONE else "open"
    tasks = await db.list_tasks(config.allowed_user_id, status)
    names = await category_names(db, config.allowed_user_id)
    if cat == CAT_NONE:
        tasks = [task for task in tasks if task.category_id is None]
    elif cat.isdigit():
        if int(cat) in names:
            tasks = [task for task in tasks if task.category_id == int(cat)]
        else:
            cat = CAT_ALL
    text, visible, page, pages = render_list(
        scope,
        tasks,
        tz=clock.tz,
        today=clock.today(),
        now_utc=clock.now_utc(),
        page=page,
    )
    if cat != CAT_ALL:
        head, _, rest = text.partition("\n")
        text = f"{head}\n🏷 {h(filter_label(names, cat))}" + (f"\n{rest}" if rest else "")
    markup = list_keyboard(visible, scope, page, pages, clock.tz, filter_label(names, cat))
    if edit:
        await safe_edit(target, text, markup or EMPTY_INLINE)
        return
    await target.answer(text, reply_markup=markup or main_menu())


async def show_week(
    target,
    db: Database,
    clock: Clock,
    config: Config,
    page: int,
    *,
    edit: bool,
) -> None:
    tasks = await db.list_tasks(config.allowed_user_id, "open")
    names = await category_names(db, config.allowed_user_id)
    from taskbot.week import count_earlier_overdue

    text, visible, page, pages = render_week(
        tasks,
        tz=clock.tz,
        today=clock.today(),
        now_utc=clock.now_utc(),
        page=page,
        categories=names,
        earlier_overdue=count_earlier_overdue(tasks, clock.today(), clock.now_utc()),
    )
    markup = list_keyboard(visible, SCOPE_WEEK, page, pages, clock.tz)
    if edit:
        await safe_edit(target, text, markup or EMPTY_INLINE)
        return
    await target.answer(text, reply_markup=markup or main_menu())


async def show_card(
    target,
    task: Task,
    clock: Clock,
    scope: str,
    page: int,
    *,
    edit: bool,
    notice: str | None = None,
    db: Database | None = None,
) -> None:
    category = None
    if db is not None and task.category_id is not None:
        category = (await category_names(db, task.owner_id)).get(task.category_id)
    text = render_card(
        task,
        clock.tz,
        today=clock.today(),
        now_utc=clock.now_utc(),
        notice=notice,
        category=category,
    )
    markup = card_keyboard(task, scope, page)
    if edit:
        await safe_edit(target, text, markup)
        return
    await target.answer(text, reply_markup=markup)
