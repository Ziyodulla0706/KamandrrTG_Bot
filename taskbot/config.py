import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
DEFAULT_TIMEZONE = "Asia/Tashkent"
DEFAULT_DATABASE_PATH = "./data/tasks.sqlite3"


class ConfigError(Exception):
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_id: int
    timezone: ZoneInfo
    database_path: Path

    def __repr__(self) -> str:
        return (
            "Config("
            f"allowed_user_id={self.allowed_user_id}, "
            f"timezone={self.timezone.key}, "
            f"database_path={self.database_path}, "
            "bot_token='***')"
        )


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    if environ is None:
        load_dotenv(ROOT / ".env", override=False)
        environ = os.environ

    messages: list[str] = []
    token = environ.get("BOT_TOKEN", "").strip()
    if not token:
        messages.append("Укажите BOT_TOKEN в файле .env. Токен выдаёт @BotFather.")
    elif TOKEN_RE.fullmatch(token) is None:
        messages.append(
            "BOT_TOKEN задан, но не похож на токен Telegram. "
            "Проверьте значение в .env — оно сюда не выводится."
        )

    raw_user = environ.get("ALLOWED_USER_ID", "").strip()
    user_id = 0
    if not raw_user:
        messages.append(
            "Укажите ALLOWED_USER_ID в файле .env — числовой Telegram ID владельца."
        )
    else:
        try:
            user_id = int(raw_user)
        except ValueError:
            messages.append("ALLOWED_USER_ID должен быть целым числом.")
        else:
            if user_id <= 0:
                messages.append("ALLOWED_USER_ID должен быть положительным числом.")

    raw_tz = environ.get("TIMEZONE", "").strip() or DEFAULT_TIMEZONE
    try:
        timezone = ZoneInfo(raw_tz)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo(DEFAULT_TIMEZONE)
        messages.append(
            f"TIMEZONE «{raw_tz}» не распознан. Пример: Asia/Tashkent."
        )

    raw_path = environ.get("DATABASE_PATH", "").strip() or DEFAULT_DATABASE_PATH
    database_path = Path(raw_path).expanduser()
    if not database_path.is_absolute():
        database_path = Path.cwd() / database_path

    if messages:
        raise ConfigError(messages)

    return Config(
        bot_token=token,
        allowed_user_id=user_id,
        timezone=timezone,
        database_path=database_path,
    )
