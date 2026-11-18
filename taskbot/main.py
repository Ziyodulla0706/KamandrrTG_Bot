import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from taskbot.access import AccessMiddleware
from taskbot.config import Config, ConfigError, load_config
from taskbot.dates import Clock
from taskbot.db import Database
from taskbot.handlers import setup_router
from taskbot.locks import UserLocks
from taskbot.schedule import run_tick
from taskbot.worker import BotSender, poll_with_worker

# aiogram 3.32 сам не удаляет очередь updates. Webhook снимаем явно,
# но накопленные сообщения оставляем.
DROP_PENDING_UPDATES = False


class RedactSecrets(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [item for item in secrets if item]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(bot_token: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    redact = RedactSecrets([bot_token])
    logging.getLogger().addFilter(redact)
    for name in ("aiogram", "aiogram.event", "aiogram.dispatcher"):
        logging.getLogger(name).addFilter(redact)
    logging.getLogger("aiogram").setLevel(logging.WARNING)


def build_dispatcher(config: Config) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    access = AccessMiddleware(config.allowed_user_id)
    dispatcher.message.middleware(access)
    dispatcher.callback_query.middleware(access)
    dispatcher.include_router(setup_router())
    return dispatcher


async def prepare_polling(bot: Bot) -> None:
    await bot.delete_webhook(drop_pending_updates=DROP_PENDING_UPDATES)


async def run(config: Config) -> None:
    setup_logging(config.bot_token)
    log = logging.getLogger("taskbot")
    log.info("Часовой пояс: %s", config.timezone.key)
    log.info("База: %s", config.database_path)
    database = Database(config.database_path)
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = build_dispatcher(config)
    clock = Clock(config.timezone)
    locks = UserLocks()
    sender = BotSender(bot)

    async def tick() -> None:
        async with locks.lock(config.allowed_user_id):
            await run_tick(database, clock, config, sender)

    async def polling() -> None:
        await dispatcher.start_polling(
            bot,
            close_bot_session=False,
            db=database,
            clock=clock,
            config=config,
            locks=locks,
        )

    try:
        await database.connect()
        await database.migrate()
        await prepare_polling(bot)
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Показать меню и примеры"),
                BotCommand(command="cancel", description="Остановить текущий диалог"),
            ]
        )
        await poll_with_worker(polling, tick, secret=config.bot_token)
    finally:
        await database.close()
        await bot.session.close()


def main() -> None:
    if sys.version_info < (3, 12):
        print("Нужен Python 3.12 или новее.", file=sys.stderr)
        raise SystemExit(1)
    try:
        config = load_config()
    except ConfigError as exc:
        print("Не удалось запустить бота. Проверьте файл .env:", file=sys.stderr)
        for line in exc.messages:
            print(f"— {line}", file=sys.stderr)
        raise SystemExit(1) from exc
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        print("Бот остановлен.")


if __name__ == "__main__":
    main()
