import logging
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from taskbot.config import Config
from taskbot.dates import Clock
from taskbot.db import Database
from taskbot.export import take_snapshot, write_export
from taskbot.locks import UserLocks
from taskbot.schedule import safe_error
from taskbot.texts import STALE

router = Router(name="export")
log = logging.getLogger("taskbot.export")

_CAPTIONS = {
    "csv": "Задачи в CSV (UTF-8 с BOM).",
    "txt": "Заметки в TXT.",
    "zip": "Задачи, заметки и расписания повторений.",
}


async def show_export(message) -> None:
    await message.answer(
        "📤 Экспорт. Что выгрузить?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Задачи CSV", callback_data="ex:csv"),
                    InlineKeyboardButton(text="Заметки TXT", callback_data="ex:txt"),
                ],
                [InlineKeyboardButton(text="Всё ZIP", callback_data="ex:zip")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("ex:"))
async def on_export(
    callback,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    kind = (callback.data or "").split(":", 1)[-1]
    if kind not in _CAPTIONS:
        await callback.answer(STALE, show_alert=True)
        return
    await callback.answer("Готовлю файл…")
    async with locks.lock(config.allowed_user_id):
        try:
            snapshot = await take_snapshot(db, config.allowed_user_id, clock)
        except Exception as exc:
            log.warning("Снимок для экспорта не получен: %s", safe_error(exc, config.bot_token))
            await callback.message.answer("Не удалось подготовить файл. Попробуйте ещё раз.")
            return
    # Временный каталог удаляется при выходе из блока, в том числе при ошибке отправки.
    try:
        with tempfile.TemporaryDirectory(prefix="taskbot-export-") as folder:
            path: Path = write_export(kind, snapshot, Path(folder))
            await callback.message.answer_document(
                FSInputFile(path),
                caption=_CAPTIONS[kind],
            )
    except Exception as exc:
        log.warning("Файл экспорта не отправлен: %s", safe_error(exc, config.bot_token))
        await callback.message.answer("Файл не отправлен. Попробуйте ещё раз.")
        return
    await callback.message.answer("Файл отправлен.")
