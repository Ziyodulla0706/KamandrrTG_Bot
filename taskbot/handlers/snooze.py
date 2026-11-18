from aiogram import F, Router

from taskbot.config import Config
from taskbot.dates import Clock
from taskbot.db import Database
from taskbot.locks import UserLocks
from taskbot.schedule import place_snooze

router = Router(name="snooze")


@router.callback_query(F.data.startswith("sn:"))
async def on_snooze(
    callback,
    db: Database,
    clock: Clock,
    config: Config,
    locks: UserLocks,
) -> None:
    parsed = _parse(callback.data or "")
    if parsed is None:
        await callback.answer("Это действие уже неактуально.", show_alert=True)
        return
    delivery_id, minutes = parsed
    async with locks.lock(config.allowed_user_id):
        result = await place_snooze(db, clock, config, delivery_id, minutes)
        if result.code == "scheduled":
            await callback.answer("Напоминание отложено")
            await callback.message.answer(result.text)
            return
        if result.code == "already":
            await callback.answer()
            await callback.message.answer(result.text)
            return
        await callback.answer(result.text, show_alert=True)


def _parse(data: str) -> tuple[int, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "sn":
        return None
    try:
        delivery_id = int(parts[1])
        minutes = int(parts[2])
    except ValueError:
        return None
    if minutes not in {15, 60} or delivery_id <= 0:
        return None
    return delivery_id, minutes
