from pathlib import Path

from taskbot.main import DROP_PENDING_UPDATES, prepare_polling


def test_polling_does_not_drop_pending_updates():
    assert DROP_PENDING_UPDATES is False

    class Bot:
        def __init__(self) -> None:
            self.drop = None

        async def delete_webhook(self, drop_pending_updates=None):
            self.drop = drop_pending_updates

    bot = Bot()

    import asyncio

    asyncio.run(prepare_polling(bot))
    assert bot.drop is False


def test_code_does_not_use_naive_local_dates_or_aiogram2():
    root = Path(__file__).resolve().parents[1] / "taskbot"
    sources = [path.read_text(encoding="utf-8") for path in root.rglob("*.py")]
    combined = "\n".join(sources)
    assert "date.today(" not in combined
    assert "datetime.utcnow(" not in combined
    assert "datetime.now()" not in combined
    assert "from aiogram.utils import executor" not in combined
    assert "drop_pending_updates=True" not in combined
    assert "aiogram" in combined
