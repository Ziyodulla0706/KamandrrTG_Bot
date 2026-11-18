from dataclasses import dataclass
from datetime import date, time

from taskbot.config import Config
from taskbot.dates import Clock, to_utc
from taskbot.db import Database, Task
from taskbot.parsing import QUEUE_KINDS, REJECTED_KINDS, OK, parse_line, payload_from_parsed


@dataclass
class Prepared:
    ready: list[dict]
    queue: list[dict]
    rejected: list[dict]


def split_lines(text: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        cleaned = line.strip()
        if cleaned:
            rows.append((number, cleaned))
    return rows


def prepare_message(text: str, *, today: date) -> Prepared:
    ready: list[dict] = []
    queue: list[dict] = []
    rejected: list[dict] = []
    for number, line in split_lines(text):
        parsed = parse_line(line, today=today)
        payload = payload_from_parsed(number, parsed)
        if parsed.kind == OK:
            ready.append(payload)
        elif parsed.kind in QUEUE_KINDS:
            queue.append(payload)
        elif parsed.kind in REJECTED_KINDS:
            rejected.append(payload)
        else:
            rejected.append(payload)
    return Prepared(ready=ready, queue=queue, rejected=rejected)


def time_from_payload(payload: dict) -> time | None:
    raw = payload.get("time")
    if not raw:
        return None
    hour, minute = str(raw).split(":", maxsplit=1)
    return time(int(hour), int(minute))


async def save_payload(
    db: Database,
    config: Config,
    clock: Clock,
    payload: dict,
    task_date: date,
    chat_id: int,
    message_id: int,
    note_id: int | None = None,
) -> tuple[Task, bool]:
    clock_time = time_from_payload(payload)
    due_at = to_utc(task_date, clock_time, clock.tz) if clock_time is not None else None
    return await db.add_task(
        owner_id=config.allowed_user_id,
        title=payload["title"],
        source_text=payload["source_text"],
        task_date=task_date,
        due_at_utc=due_at,
        now=clock.now_utc(),
        source_chat_id=chat_id,
        source_message_id=message_id,
        source_line=int(payload["line"]),
        note_id=note_id,
    )


async def persist_ready(
    db: Database,
    config: Config,
    clock: Clock,
    ready: list[dict],
    chat_id: int,
    message_id: int,
    note_id: int | None = None,
) -> tuple[list[Task], list[Task]]:
    saved: list[Task] = []
    already: list[Task] = []
    for payload in ready:
        if not payload.get("proposed_date"):
            continue
        task, created = await save_payload(
            db,
            config,
            clock,
            payload,
            date.fromisoformat(payload["proposed_date"]),
            chat_id,
            message_id,
            note_id,
        )
        if created:
            saved.append(task)
        else:
            already.append(task)
    return saved, already
