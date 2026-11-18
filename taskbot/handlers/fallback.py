from aiogram import Router
from aiogram.filters import StateFilter

from taskbot.keyboards import main_menu
from taskbot.texts import STALE

router = Router(name="fallback")


@router.callback_query()
async def on_unknown_callback(callback) -> None:
    await callback.answer(STALE, show_alert=True)


@router.message(StateFilter(None))
async def on_unsupported(message) -> None:
    await message.answer("Пришлите задачу обычным текстом.", reply_markup=main_menu())
