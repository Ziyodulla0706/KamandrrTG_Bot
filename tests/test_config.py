from pathlib import Path
from zoneinfo import ZoneInfo

from taskbot.config import ConfigError, load_config
from taskbot.main import RedactSecrets
import logging


TOKEN = "123456789:" + ("B" * 35)


def test_valid_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(
        {
            "BOT_TOKEN": TOKEN,
            "ALLOWED_USER_ID": "42",
            "TIMEZONE": "Asia/Tashkent",
            "DATABASE_PATH": "./data/tasks.sqlite3",
        }
    )
    assert config.allowed_user_id == 42
    assert config.timezone == ZoneInfo("Asia/Tashkent")
    assert config.database_path == Path.cwd() / "data" / "tasks.sqlite3"
    assert "B" * 10 not in repr(config)


def test_errors_do_not_reveal_the_token():
    secret = "super-secret-token-value"
    try:
        load_config({"BOT_TOKEN": secret, "ALLOWED_USER_ID": "нет", "TIMEZONE": "Not/AZone"})
    except ConfigError as exc:
        text = "\n".join(exc.messages)
    else:
        raise AssertionError("ожидалась ошибка конфигурации")
    assert secret not in text
    assert "BOT_TOKEN" in text
    assert "ALLOWED_USER_ID" in text
    assert "Not/AZone" in text


def test_missing_values_are_explained(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    try:
        load_config({})
    except ConfigError as exc:
        text = "\n".join(exc.messages)
    else:
        raise AssertionError("ожидалась ошибка")
    assert "BOT_TOKEN" in text
    assert "ALLOWED_USER_ID" in text


def test_blank_timezone_and_path_use_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config({"BOT_TOKEN": TOKEN, "ALLOWED_USER_ID": "7", "TIMEZONE": "", "DATABASE_PATH": ""})
    assert config.timezone.key == "Asia/Tashkent"
    assert config.database_path.name == "tasks.sqlite3"


def test_log_filter_redacts_token():
    secret = "123456:SECRETVALUE"
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "url /bot%s/getMe", (secret,), None)
    assert RedactSecrets([secret]).filter(record) is True
    assert secret not in record.getMessage()
    assert "***" in record.getMessage()
