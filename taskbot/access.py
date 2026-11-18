import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

log = logging.getLogger(__name__)

ALLOW = "allow"
NOT_PRIVATE = "not_private"
DENIED = "denied"

PRIVATE_ONLY = "Я работаю только в личном чате."
DENIED_TEXT = "Этот бот личный и отвечает только владельцу."


def decide_access(user_id: int | None, chat_type: str | None, allowed_user_id: int) -> str:
    """Владелец задаётся только в ALLOWED_USER_ID. Первый написавший им не становится."""

    if chat_type != "private":
        return NOT_PRIVATE
    if user_id is None or user_id != allowed_user_id:
        return DENIED
    return ALLOW


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_id: int) -> None:
        self.allowed_user_id = allowed_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id, chat_type = _actor(event)
        decision = decide_access(user_id, chat_type, self.allowed_user_id)
        if decision == ALLOW:
            return await handler(event, data)
        log.info("Отказ в доступе: %s", decision)
        await _reject(event, PRIVATE_ONLY if decision == NOT_PRIVATE else DENIED_TEXT)
        return None


def _actor(event: TelegramObject) -> tuple[int | None, str | None]:
    user = getattr(event, "from_user", None)
    user_id = getattr(user, "id", None) if user is not None else None
    chat = getattr(event, "chat", None)
    if chat is None:
        message = getattr(event, "message", None)
        chat = getattr(message, "chat", None) if message is not None else None
    chat_type = getattr(chat, "type", None) if chat is not None else None
    if chat_type is not None:
        chat_type = str(chat_type)
    return user_id, chat_type


async def _reject(event: TelegramObject, text: str) -> None:
    if getattr(event, "data", None) is not None and hasattr(event, "answer"):
        await event.answer(text, show_alert=True)
        return
    answer = getattr(event, "answer", None)
    if answer is not None:
        await answer(text)
