from datetime import time
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from taskbot.dates import from_utc
from taskbot.db import Task
from taskbot.texts import (
    BTN_ADD,
    BTN_ALL,
    BTN_CATS,
    BTN_DONE,
    BTN_EXPORT,
    BTN_NOTES,
    BTN_RECUR,
    BTN_SEARCH,
    BTN_SETTINGS,
    BTN_TODAY,
    BTN_TOMORROW,
    BTN_WEEK,
)

EMPTY_INLINE = InlineKeyboardMarkup(inline_keyboard=[])


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_NOTES)],
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
            [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_ALL)],
            [KeyboardButton(text=BTN_DONE), KeyboardButton(text=BTN_SEARCH)],
            [KeyboardButton(text=BTN_RECUR), KeyboardButton(text=BTN_CATS)],
            [KeyboardButton(text=BTN_EXPORT), KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def date_choice_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data=f"c:today:{index}"),
                InlineKeyboardButton(text="Завтра", callback_data=f"c:tomorrow:{index}"),
            ],
            [
                InlineKeyboardButton(text="Другая дата", callback_data=f"c:other:{index}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"c:cancel:{index}"),
            ],
        ]
    )


def past_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Исправить", callback_data=f"c:fix:{index}")],
            [
                InlineKeyboardButton(
                    text="Сохранить как просроченную",
                    callback_data=f"c:overdue:{index}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=f"c:cancel:{index}")],
        ]
    )


def clarify_cancel_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data=f"c:cancel:{index}")]
        ]
    )


def saved_keyboard(tasks: list[Task]) -> InlineKeyboardMarkup:
    rows = []
    single = len(tasks) == 1
    for task in tasks:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Открыть задачу" if single else _clip(f"Открыть: {task.title}"),
                    callback_data=f"o:al:0:{task.id}",
                )
            ]
        )
        if single:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✏️ Изменить",
                        callback_data=f"n:{task.id}:al:0",
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_keyboard(task: Task, scope: str, page: int) -> InlineKeyboardMarkup:
    if task.status == "open":
        primary = InlineKeyboardButton(
            text="✅ Готово",
            callback_data=f"d:{task.id}:{scope}:{page}",
        )
    else:
        primary = InlineKeyboardButton(
            text="↩️ Вернуть в работу",
            callback_data=f"u:{task.id}:{scope}:{page}",
        )
    extra = [
        InlineKeyboardButton(
            text="🏷 Категория",
            callback_data=f"cs:t:{task.id}:{scope}{page}",
        )
    ]
    if task.note_id is not None:
        extra.append(
            InlineKeyboardButton(
                text="📝 Заметка",
                callback_data=f"nt:o:{task.note_id}:n0",
            )
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [primary],
            [
                InlineKeyboardButton(
                    text="📅 Перенести",
                    callback_data=f"m:{task.id}:{scope}:{page}",
                ),
                InlineKeyboardButton(
                    text="✏️ Изменить",
                    callback_data=f"n:{task.id}:{scope}:{page}",
                ),
            ],
            extra,
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"x:{task.id}:{scope}:{page}",
                ),
                InlineKeyboardButton(
                    text="⬅️ К списку",
                    callback_data=f"l:{scope}:{page}",
                ),
            ],
        ]
    )


def series_choice_keyboard(
    task_id: int,
    series_id: int,
    scope: str,
    page: int,
    *,
    action: str,
) -> InlineKeyboardMarkup:
    """Явный выбор: только эта задача, серия или отмена."""

    one = {
        "delete": f"y:{task_id}",
        "rename": f"n1:{task_id}:{scope}:{page}",
        "move": f"m1:{task_id}:{scope}:{page}",
    }[action]
    if action == "delete":
        series_button = InlineKeyboardButton(text="Удалить серию", callback_data=f"rc:del:{series_id}")
    else:
        series_button = InlineKeyboardButton(text="Изменить серию", callback_data=f"rc:ed:{series_id}")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Только эту задачу", callback_data=one)],
            [series_button],
            [InlineKeyboardButton(text="Отмена", callback_data=f"z:{task_id}:{scope}:{page}")],
        ]
    )


def delete_keyboard(task_id: int, scope: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Удалить", callback_data=f"y:{task_id}")],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"z:{task_id}:{scope}:{page}",
                )
            ],
        ]
    )


def move_day_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data=f"r:today:{task_id}"),
                InlineKeyboardButton(text="Завтра", callback_data=f"r:tomorrow:{task_id}"),
            ],
            [
                InlineKeyboardButton(text="Послезавтра", callback_data=f"r:after:{task_id}"),
                InlineKeyboardButton(text="Другая дата", callback_data=f"r:other:{task_id}"),
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=f"r:cancel:{task_id}")],
        ]
    )


def move_past_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Исправить", callback_data=f"r:fix:{task_id}")],
            [
                InlineKeyboardButton(
                    text="Сохранить как просроченную",
                    callback_data=f"r:overdue:{task_id}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=f"r:cancel:{task_id}")],
        ]
    )


def move_time_keyboard(task_id: int, old_time: str | None) -> InlineKeyboardMarkup:
    rows = []
    if old_time:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Оставить {old_time}",
                    callback_data=f"r:keep:{task_id}",
                )
            ]
        )
        rows.append(
            [InlineKeyboardButton(text="Другое время", callback_data=f"r:set:{task_id}")]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="Указать время", callback_data=f"r:set:{task_id}")]
        )
    rows.append(
        [InlineKeyboardButton(text="Без времени", callback_data=f"r:none:{task_id}")]
    )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"r:cancel:{task_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def move_cancel_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data=f"r:cancel:{task_id}")]
        ]
    )


def rename_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data=f"e:cancel:{task_id}")]
        ]
    )


def list_keyboard(
    tasks: list[Task],
    scope: str,
    page: int,
    pages: int,
    tz: ZoneInfo,
    cat_label: str | None = None,
) -> InlineKeyboardMarkup | None:
    rows = []
    if cat_label is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_clip(f"🏷 Категория: {cat_label}"),
                    callback_data=f"tf:{scope}:pick",
                )
            ]
        )
    for task in tasks:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_task_button(task, tz),
                    callback_data=f"o:{scope}:{page}:{task.id}",
                )
            ]
        )
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(text="◀️", callback_data=f"l:{scope}:{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop")
        )
        if page + 1 < pages:
            nav.append(
                InlineKeyboardButton(text="▶️", callback_data=f"l:{scope}:{page + 1}")
            )
        rows.append(nav)
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _task_button(task: Task, tz: ZoneInfo) -> str:
    if task.due_at_utc is None:
        when = "без времени"
    else:
        clock: time = from_utc(task.due_at_utc, tz).time().replace(tzinfo=None)
        when = clock.strftime("%H:%M")
    return _clip(f"{task.task_date.strftime('%d.%m')} {when} {task.title}")


def _clip(text: str, limit: int = 64) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
