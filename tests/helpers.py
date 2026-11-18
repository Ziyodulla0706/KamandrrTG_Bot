from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from taskbot.config import Config
from taskbot.dates import Clock
from taskbot.db import Database
from taskbot.locks import UserLocks

TZ = ZoneInfo("Asia/Tashkent")


def clock_at(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> Clock:
    moment = datetime(year, month, day, hour, minute, tzinfo=TZ)
    return Clock(TZ, now=moment)


def make_config(path, user_id: int = 42) -> Config:
    return Config(
        bot_token="123456789:" + ("A" * 35),
        allowed_user_id=user_id,
        timezone=TZ,
        database_path=path,
    )


async def open_db(path) -> Database:
    database = Database(path)
    await database.connect()
    await database.migrate()
    return database


class FakeState:
    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def get_state(self):
        return self.state

    async def set_state(self, state=None) -> None:
        if state is None:
            self.state = None
        else:
            self.state = state.state if hasattr(state, "state") else state

    async def get_data(self) -> dict:
        return dict(self.data)

    async def set_data(self, data: dict) -> None:
        self.data = dict(data)

    async def update_data(self, data=None, **kwargs):
        if data:
            self.data.update(data)
        self.data.update(kwargs)
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


class FakeMessage:
    def __init__(self, text: str = "", message_id: int = 10, chat_id: int = 77, user_id: int = 42):
        self.text = text
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[dict] = []
        self.edits: list[dict] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append({"text": text, "markup": reply_markup})
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append({"text": text, "markup": reply_markup})
        return self

    async def answer_document(self, document, caption=None, **kwargs):
        if getattr(self, "fail_documents", False):
            raise RuntimeError("send failed")
        path = document.path
        self.documents = getattr(self, "documents", [])
        self.documents.append(
            {"name": path.name, "path": path, "data": path.read_bytes(), "caption": caption}
        )
        return self


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage, user_id: int = 42):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)
        self.alerts: list[dict] = []

    async def answer(self, text=None, show_alert: bool = False, **kwargs):
        self.alerts.append({"text": text, "show_alert": show_alert})


def button_rows(markup) -> list[tuple[str, str | None]]:
    if markup is None or not getattr(markup, "inline_keyboard", None):
        return []
    rows = []
    for row in markup.inline_keyboard:
        for button in row:
            rows.append((button.text, button.callback_data))
    return rows


def new_locks() -> UserLocks:
    return UserLocks()
