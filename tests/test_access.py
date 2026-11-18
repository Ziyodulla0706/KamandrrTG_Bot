import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from taskbot.access import ALLOW, DENIED, DENIED_TEXT, NOT_PRIVATE, PRIVATE_ONLY, AccessMiddleware, decide_access


def test_owner_comes_only_from_configuration():
    assert decide_access(5, "private", 5) == ALLOW
    assert decide_access(1, "private", 5) == DENIED
    assert decide_access(None, "private", 5) == DENIED
    assert decide_access(5, "group", 5) == NOT_PRIVATE
    assert decide_access(5, "supergroup", 5) == NOT_PRIVATE
    assert "5" not in DENIED_TEXT


def test_middleware_blocks_other_user_and_group_chat():
    asyncio.run(_middleware())


async def _middleware():
    middleware = AccessMiddleware(5)

    async def handler(event, data):
        handler.called = True
        return "ok"

    handler.called = False
    stranger = SimpleNamespace(
        from_user=SimpleNamespace(id=9),
        chat=SimpleNamespace(type="private"),
        answer=AsyncMock(),
    )
    result = await middleware(handler, stranger, {})
    assert result is None
    assert handler.called is False
    stranger.answer.assert_awaited_once()
    assert stranger.answer.await_args.args[0] == DENIED_TEXT

    handler.called = False
    group = SimpleNamespace(
        from_user=SimpleNamespace(id=5),
        chat=SimpleNamespace(type="group"),
        answer=AsyncMock(),
    )
    await middleware(handler, group, {})
    assert handler.called is False
    assert group.answer.await_args.args[0] == PRIVATE_ONLY

    handler.called = False
    callback = SimpleNamespace(
        data="d:1:td:0",
        from_user=SimpleNamespace(id=9),
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )
    await middleware(handler, callback, {})
    assert handler.called is False
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs["show_alert"] is True

    owner = SimpleNamespace(
        from_user=SimpleNamespace(id=5),
        chat=SimpleNamespace(type="private"),
        answer=AsyncMock(),
    )
    assert await middleware(handler, owner, {}) == "ok"
    assert handler.called is True
    owner.answer.assert_not_awaited()
