import asyncio


class UserLocks:
    """Один замок на пользователя, чтобы повторное нажатие не шло параллельно."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, user_id: int) -> asyncio.Lock:
        current = self._locks.get(user_id)
        if current is None:
            current = asyncio.Lock()
            self._locks[user_id] = current
        return current
