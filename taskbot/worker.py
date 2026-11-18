import asyncio
import logging

from aiogram import Bot

from taskbot.schedule import safe_error

POLL_SECONDS = 12
log = logging.getLogger("taskbot.worker")


class BotSender:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, chat_id: int, text: str, reply_markup=None) -> None:
        await self.bot.send_message(chat_id, text, reply_markup=reply_markup)


async def notification_worker(
    stop: asyncio.Event,
    tick,
    *,
    poll_seconds: float = POLL_SECONDS,
    secret: str = "",
) -> None:
    while not stop.is_set():
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Сбой прохода уведомлений: %s", safe_error(exc, secret))
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue
        except asyncio.CancelledError:
            raise


async def poll_with_worker(polling, tick, *, poll_seconds: float = POLL_SECONDS, secret: str = "") -> None:
    stop = asyncio.Event()
    worker = asyncio.create_task(
        notification_worker(stop, tick, poll_seconds=poll_seconds, secret=secret)
    )
    try:
        await polling()
    finally:
        stop.set()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
