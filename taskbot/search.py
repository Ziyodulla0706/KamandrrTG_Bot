from dataclasses import dataclass
from datetime import datetime

from taskbot.db import Database
from taskbot.notes import first_line, list_notes

MAX_QUERY = 200
SNIPPET_RADIUS = 30


@dataclass
class Hit:
    kind: str  # task | note
    id: int
    title: str
    snippet: str
    status: str | None  # open | done для задач
    updated_at: datetime


def normalize_query(raw: str) -> str:
    return " ".join((raw or "").split())[:MAX_QUERY]


def contains(haystack: str, needle_folded: str) -> bool:
    """Буквальное вхождение без учёта регистра. Подстановочных символов нет."""

    return needle_folded in haystack.casefold()


def make_snippet(text: str, needle: str) -> str:
    flat = " ".join(text.split())
    index = flat.casefold().find(needle.casefold())
    if index < 0:
        index = 0
    start = max(0, index - SNIPPET_RADIUS)
    end = min(len(flat), index + len(needle) + SNIPPET_RADIUS)
    piece = flat[start:end]
    return ("…" if start > 0 else "") + piece + ("…" if end < len(flat) else "")


async def search(db: Database, owner_id: int, raw_query: str) -> list[Hit]:
    query = normalize_query(raw_query)
    if not query:
        return []
    needle = query.casefold()
    hits: list[Hit] = []
    for task in await db.list_all_tasks(owner_id):
        if contains(task.title, needle):
            hits.append(
                Hit("task", task.id, task.title, make_snippet(task.title, query), task.status, task.updated_at)
            )
    for note in await list_notes(db, owner_id):
        if contains(note.text, needle):
            hits.append(
                Hit(
                    "note",
                    note.id,
                    first_line(note.text),
                    make_snippet(note.text, query),
                    None,
                    note.updated_at,
                )
            )
    hits.sort(key=lambda hit: (hit.updated_at, hit.id), reverse=True)
    return hits
