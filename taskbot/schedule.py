"""Планирование сводки и напоминаний.

Одна и та же запись в обычном сценарии не отправляется дважды: worker сначала
переводит её в processing. Если Telegram уже принял сообщение, а процесс
завершился до записи результата, после восстановления возможна редкая повторная
отправка. Это не гарантия exactly-once.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)

from taskbot.categories import seed_defaults
from taskbot.config import Config
from taskbot.dates import Clock, dump_dt, to_utc
from taskbot.recurrence import generate_horizon
from taskbot.db import Database, Delivery, Settings, Task
from taskbot.notify import digest_keyboard, reminder_keyboard, render_digest, render_reminder, render_snooze_confirmation

DIGEST_GRACE = timedelta(hours=2)
REMINDER_GRACE = timedelta(minutes=15)
STUCK_AFTER = timedelta(minutes=2)
MAX_TEMPORARY_ATTEMPTS = 5
MAX_RATE_ATTEMPTS = 8
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 15 * 60
REOPEN_REASONS = {"rescheduled", "reminders_off", "task_closed"}
SNOOZE_MINUTES = (15, 60)

_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_PERMANENT_TEXT = (
    "bot was blocked",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation",
    "forbidden",
)
log = logging.getLogger("taskbot.schedule")


@dataclass
class SnoozeResult:
    code: str
    text: str
    delivery_id: int | None = None


def reminder_key(task_id: int, due: datetime) -> str:
    return f"reminder:{task_id}:{dump_dt(_floor(due))}"


def digest_key(day) -> str:
    return f"digest:{day.isoformat()}"


def snooze_key(task_id: int, source_delivery_id: int, minutes: int) -> str:
    return f"snooze:{task_id}:from:{source_delivery_id}:{minutes}"


async def run_tick(db: Database, clock: Clock, config: Config, sender) -> None:
    now = _floor(clock.now_utc())
    await db.recover_stuck(config.allowed_user_id, now, STUCK_AFTER)
    await seed_defaults(db, config.allowed_user_id, now)
    # Горизонт повторений достраивается от сегодняшнего дня. Повторный вызов безопасен.
    await generate_horizon(db, clock, config.allowed_user_id)
    await sync_plan(db, clock, config)
    claimed = await db.claim_due(config.allowed_user_id, now)
    for delivery in claimed:
        await dispatch_one(db, clock, config, sender, delivery)


async def sync_plan(db: Database, clock: Clock, config: Config) -> None:
    now = _floor(clock.now_utc())
    settings = await db.ensure_settings(config.allowed_user_id, now)
    await _sync_digest(db, clock, config, settings, now)
    await _sync_reminders(db, clock, config, settings, now)


async def dispatch_one(
    db: Database,
    clock: Clock,
    config: Config,
    sender,
    delivery: Delivery,
) -> None:
    now = _floor(clock.now_utc())
    try:
        await _dispatch(db, clock, config, sender, delivery, now)
    except Exception as exc:
        await _fail(db, config, delivery, exc, now)


async def place_snooze(
    db: Database,
    clock: Clock,
    config: Config,
    source_delivery_id: int,
    minutes: int,
) -> SnoozeResult:
    if minutes not in SNOOZE_MINUTES:
        return SnoozeResult("stale", "Это действие уже неактуально.")
    owner = config.allowed_user_id
    now = _floor(clock.now_utc())
    settings = await db.ensure_settings(owner, now)
    if not settings.reminders_enabled:
        return SnoozeResult("disabled", "Напоминания выключены в настройках.")
    source = await db.get_delivery(owner, source_delivery_id)
    if source is None or source.kind not in {"reminder", "snooze"} or source.task_id is None:
        return SnoozeResult("stale", "Это действие уже неактуально.")
    task = await db.get_task(owner, source.task_id)
    if task is None:
        return SnoozeResult("missing", "Задача не найдена или уже удалена.")
    if task.status != "open":
        return SnoozeResult("closed", "Задача уже выполнена.")
    if task.due_at_utc is None or source.task_due_at_utc is None:
        return SnoozeResult("stale", "Это действие уже неактуально.")
    if dump_dt(_floor(task.due_at_utc)) != dump_dt(_floor(source.task_due_at_utc)):
        return SnoozeResult("stale", "Срок задачи уже изменился.")

    when = now + timedelta(minutes=minutes)
    key = snooze_key(task.id, source.id, minutes)
    existing = await db.get_delivery_by_key(owner, key)
    if existing is not None and existing.status == "sent":
        return SnoozeResult(
            "sent",
            "Это отложенное напоминание уже отправлено.",
            existing.id,
        )
    if existing is not None and existing.status in {"pending", "processing"}:
        return SnoozeResult(
            "already",
            render_snooze_confirmation(task, clock.tz, existing.scheduled_for, already=True),
            existing.id,
        )

    await db.cancel_open_deliveries(
        owner,
        now=now,
        reason="replaced",
        kinds=("snooze",),
        task_id=task.id,
    )
    if existing is not None and existing.status in {"cancelled", "skipped", "blocked"}:
        reopened = await db.reopen_delivery(
            owner,
            existing.id,
            scheduled_for=when,
            task_due_at_utc=task.due_at_utc,
            now=now,
            only_statuses=("cancelled", "skipped", "blocked"),
        )
        if reopened:
            return SnoozeResult(
                "scheduled",
                render_snooze_confirmation(task, clock.tz, when, already=False),
                existing.id,
            )
        existing = await db.get_delivery(owner, existing.id)
        if existing is not None and existing.status in {"pending", "processing", "sent"}:
            return _existing_snooze_result(existing, task, clock)
    delivery, _created = await db.insert_delivery(
        owner_id=owner,
        kind="snooze",
        due_key=key,
        scheduled_for=when,
        now=now,
        task_id=task.id,
        task_due_at_utc=task.due_at_utc,
    )
    if delivery.status == "sent":
        return SnoozeResult("sent", "Это отложенное напоминание уже отправлено.", delivery.id)
    if delivery.status != "pending":
        return SnoozeResult("stale", "Это действие уже неактуально.", delivery.id)
    if not _same_moment(delivery.scheduled_for, when):
        return SnoozeResult(
            "already",
            render_snooze_confirmation(task, clock.tz, delivery.scheduled_for, already=True),
            delivery.id,
        )
    return SnoozeResult(
        "scheduled",
        render_snooze_confirmation(task, clock.tz, when, already=False),
        delivery.id,
    )


def classify_delivery_error(exc: BaseException) -> tuple[str, int | None]:
    if isinstance(exc, TelegramRetryAfter):
        return "retry_after", int(exc.retry_after)
    if isinstance(
        exc,
        (
            TelegramForbiddenError,
            TelegramUnauthorizedError,
            TelegramNotFound,
            TelegramBadRequest,
            TelegramEntityTooLarge,
        ),
    ):
        return "permanent", None
    if isinstance(exc, (TelegramNetworkError, TelegramServerError)):
        return "temporary", None
    text = str(exc).lower()
    if any(marker in text for marker in _PERMANENT_TEXT):
        return "permanent", None
    return "temporary", None


def safe_error(exc: BaseException, token: str) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if token:
        text = text.replace(token, "***")
    text = _TOKEN_RE.sub("***", text)
    text = " ".join(text.split())
    return text[:300]


def retry_delay_seconds(kind: str, attempt: int, retry_after: int | None) -> int:
    if kind == "retry_after":
        return max(int(retry_after or 1), 1)
    power = max(attempt - 1, 0)
    return min(BACKOFF_BASE_SECONDS * (2**power), BACKOFF_CAP_SECONDS)


async def _sync_digest(
    db: Database,
    clock: Clock,
    config: Config,
    settings: Settings,
    now: datetime,
) -> None:
    owner = config.allowed_user_id
    today = clock.today()
    key = digest_key(today)
    digest_at = _floor(to_utc(today, settings.digest_time, clock.tz))
    phase = _digest_phase(now, digest_at)
    existing = await db.get_delivery_by_key(owner, key)
    if existing is not None and existing.status in {"sent", "skipped", "blocked"}:
        return
    if not settings.digest_enabled:
        if existing is not None and existing.status in {"pending", "processing"}:
            await db.close_unsent(
                owner,
                existing.id,
                now=now,
                status="cancelled",
                reason="digest_off",
                only_statuses=("pending", "processing"),
            )
        return
    if phase == "late":
        await _skip_digest(db, owner, existing, key, digest_at, now, "late_digest")
        return
    if existing is None:
        await db.insert_delivery(
            owner_id=owner,
            kind="digest",
            due_key=key,
            scheduled_for=digest_at,
            now=now,
        )
        return
    if existing.status == "pending" and not _same_moment(existing.scheduled_for, digest_at):
        await db.reschedule_pending(owner, existing.id, digest_at, now)
        return
    if existing.status == "cancelled":
        await db.reopen_delivery(
            owner,
            existing.id,
            scheduled_for=digest_at,
            task_due_at_utc=None,
            now=now,
            only_statuses=("cancelled",),
        )


async def _skip_digest(
    db: Database,
    owner: int,
    existing: Delivery | None,
    key: str,
    digest_at: datetime,
    now: datetime,
    reason: str,
) -> None:
    if existing is None:
        await db.insert_delivery(
            owner_id=owner,
            kind="digest",
            due_key=key,
            scheduled_for=digest_at,
            now=now,
            status="skipped",
            cancel_reason=reason,
        )
        return
    await db.close_unsent(
        owner,
        existing.id,
        now=now,
        status="skipped",
        reason=reason,
        only_statuses=("pending", "processing", "cancelled"),
    )


async def _sync_reminders(
    db: Database,
    clock: Clock,
    config: Config,
    settings: Settings,
    now: datetime,
) -> None:
    del clock
    owner = config.allowed_user_id
    if not settings.reminders_enabled:
        await db.cancel_open_deliveries(
            owner,
            now=now,
            reason="reminders_off",
            kinds=("reminder", "snooze"),
        )
        return
    open_tasks = await db.list_tasks(owner, "open")
    by_id = {task.id: task for task in open_tasks}
    pending = await db.list_deliveries(
        owner,
        statuses=("pending",),
        kinds=("reminder", "snooze"),
    )
    for delivery in pending:
        task = by_id.get(delivery.task_id or -1)
        if task is None and delivery.task_id is not None:
            task = await db.get_task(owner, delivery.task_id)
        if delivery.kind == "snooze":
            await _reconcile_snooze(db, owner, delivery, task, now)
        else:
            await _reconcile_reminder(db, owner, delivery, task, settings, now)
    for task in open_tasks:
        await _ensure_regular_reminder(db, owner, task, settings, now)


async def _reconcile_reminder(
    db: Database,
    owner: int,
    delivery: Delivery,
    task: Task | None,
    settings: Settings,
    now: datetime,
) -> None:
    if task is None or task.status != "open" or task.due_at_utc is None:
        await db.close_unsent(
            owner,
            delivery.id,
            now=now,
            status="cancelled",
            reason=_task_reason(task),
            only_statuses=("pending",),
        )
        return
    if delivery.due_key != reminder_key(task.id, task.due_at_utc):
        await db.close_unsent(
            owner,
            delivery.id,
            now=now,
            status="cancelled",
            reason="rescheduled",
            only_statuses=("pending",),
        )
        return
    due = _floor(task.due_at_utc)
    if due <= now:
        if now <= due + REMINDER_GRACE:
            if delivery.scheduled_for > now:
                await db.reschedule_pending(owner, delivery.id, now, now)
            return
        await db.close_unsent(
            owner,
            delivery.id,
            now=now,
            status="skipped",
            reason="missed_reminder",
            only_statuses=("pending",),
        )
        return
    fire = _regular_fire_at(due, settings.reminder_lead_minutes, now)
    if fire is not None and not _same_moment(delivery.scheduled_for, fire):
        await db.reschedule_pending(owner, delivery.id, fire, now)


async def _reconcile_snooze(
    db: Database,
    owner: int,
    delivery: Delivery,
    task: Task | None,
    now: datetime,
) -> None:
    reason = _task_reason(task)
    if task is None or task.status != "open" or task.due_at_utc is None:
        await db.close_unsent(
            owner,
            delivery.id,
            now=now,
            status="cancelled",
            reason=reason,
            only_statuses=("pending",),
        )
        return
    if dump_dt(_floor(task.due_at_utc)) != dump_dt(_floor(delivery.task_due_at_utc)):
        await db.close_unsent(
            owner,
            delivery.id,
            now=now,
            status="cancelled",
            reason="rescheduled",
            only_statuses=("pending",),
        )


async def _ensure_regular_reminder(
    db: Database,
    owner: int,
    task: Task,
    settings: Settings,
    now: datetime,
) -> None:
    if task.due_at_utc is None:
        return
    due = _floor(task.due_at_utc)
    if due <= now:
        return
    fire = _regular_fire_at(due, settings.reminder_lead_minutes, now)
    if fire is None:
        return
    key = reminder_key(task.id, due)
    existing = await db.get_delivery_by_key(owner, key)
    if existing is None:
        await db.insert_delivery(
            owner_id=owner,
            kind="reminder",
            due_key=key,
            scheduled_for=fire,
            now=now,
            task_id=task.id,
            task_due_at_utc=due,
        )
        return
    if existing.status == "cancelled" and existing.cancel_reason in REOPEN_REASONS:
        await db.reopen_delivery(
            owner,
            existing.id,
            scheduled_for=fire,
            task_due_at_utc=due,
            now=now,
            only_statuses=("cancelled",),
        )


async def _dispatch(
    db: Database,
    clock: Clock,
    config: Config,
    sender,
    delivery: Delivery,
    now: datetime,
) -> None:
    current = await db.get_delivery(config.allowed_user_id, delivery.id)
    if current is None or current.status != "processing":
        return
    settings = await db.ensure_settings(config.allowed_user_id, now)
    if current.kind == "digest":
        await _dispatch_digest(db, clock, config, sender, current, settings, now)
        return
    if current.kind == "snooze":
        await _dispatch_reminder(
            db, clock, config, sender, current, settings, now, snoozed=True
        )
        return
    await _dispatch_reminder(db, clock, config, sender, current, settings, now, snoozed=False)


async def _dispatch_digest(
    db: Database,
    clock: Clock,
    config: Config,
    sender,
    delivery: Delivery,
    settings: Settings,
    now: datetime,
) -> None:
    owner = config.allowed_user_id
    today = clock.today()
    if delivery.due_key != digest_key(today):
        await _settle_closed(db, owner, delivery, now, "skipped", "past_day")
        return
    if not settings.digest_enabled:
        await _settle_closed(db, owner, delivery, now, "cancelled", "digest_off")
        return
    digest_at = _floor(to_utc(today, settings.digest_time, clock.tz))
    phase = _digest_phase(now, digest_at)
    if phase == "late":
        await _settle_closed(db, owner, delivery, now, "skipped", "late_digest")
        return
    if phase == "wait":
        await db.settle_processing(
            owner,
            delivery.id,
            now=now,
            status="pending",
            attempts=delivery.attempts,
        )
        return
    tasks = await db.list_tasks(owner, "open")
    text = render_digest(tasks, clock.tz, today, now)
    if text is None:
        await _settle_closed(db, owner, delivery, now, "skipped", "empty_digest")
        return
    await sender.send(owner, text, digest_keyboard())
    await db.settle_processing(
        owner,
        delivery.id,
        now=now,
        status="sent",
        attempts=delivery.attempts,
        sent=True,
    )


async def _dispatch_reminder(
    db: Database,
    clock: Clock,
    config: Config,
    sender,
    delivery: Delivery,
    settings: Settings,
    now: datetime,
    *,
    snoozed: bool,
) -> None:
    owner = config.allowed_user_id
    if not settings.reminders_enabled:
        await _settle_closed(db, owner, delivery, now, "cancelled", "reminders_off")
        return
    if delivery.task_id is None:
        await _settle_closed(db, owner, delivery, now, "cancelled", "stale")
        return
    task = await db.get_task(owner, delivery.task_id)
    if task is None:
        await _settle_closed(db, owner, delivery, now, "cancelled", "task_deleted")
        return
    if task.status != "open":
        await _settle_closed(db, owner, delivery, now, "cancelled", "task_closed")
        return
    if task.due_at_utc is None:
        await _settle_closed(db, owner, delivery, now, "cancelled", "rescheduled")
        return
    due = _floor(task.due_at_utc)
    if snoozed:
        if dump_dt(due) != dump_dt(_floor(delivery.task_due_at_utc)):
            await _settle_closed(db, owner, delivery, now, "cancelled", "rescheduled")
            return
    elif delivery.due_key != reminder_key(task.id, due):
        await _settle_closed(db, owner, delivery, now, "cancelled", "rescheduled")
        return
    elif now > due + REMINDER_GRACE:
        await _settle_closed(db, owner, delivery, now, "skipped", "missed_reminder")
        return
    late = now > due
    text = render_reminder(task, clock.tz, late=late, snoozed=snoozed)
    await sender.send(owner, text, reminder_keyboard(task.id, delivery.id))
    await db.settle_processing(
        owner,
        delivery.id,
        now=now,
        status="sent",
        attempts=delivery.attempts,
        sent=True,
    )


async def _settle_closed(
    db: Database,
    owner: int,
    delivery: Delivery,
    now: datetime,
    status: str,
    reason: str,
) -> None:
    await db.settle_processing(
        owner,
        delivery.id,
        now=now,
        status=status,
        attempts=delivery.attempts,
        cancel_reason=reason,
    )


async def _fail(
    db: Database,
    config: Config,
    delivery: Delivery,
    exc: BaseException,
    now: datetime,
) -> None:
    current = await db.get_delivery(config.allowed_user_id, delivery.id)
    if current is None or current.status != "processing":
        return
    kind, retry_after = classify_delivery_error(exc)
    attempts = current.attempts + 1
    message = safe_error(exc, config.bot_token)
    log.warning("Отправка %s не выполнена: %s", current.id, message)
    limit = MAX_RATE_ATTEMPTS if kind == "retry_after" else MAX_TEMPORARY_ATTEMPTS
    if kind == "permanent" or attempts >= limit:
        status = "blocked" if kind == "permanent" else "skipped"
        reason = "forbidden" if kind == "permanent" else "retries_exhausted"
        await db.settle_processing(
            config.allowed_user_id,
            current.id,
            now=now,
            status=status,
            attempts=attempts,
            last_error=message,
            cancel_reason=reason,
        )
        return
    delay = retry_delay_seconds(kind, attempts, retry_after)
    await db.settle_processing(
        config.allowed_user_id,
        current.id,
        now=now,
        status="pending",
        attempts=attempts,
        next_attempt_at=now + timedelta(seconds=delay),
        last_error=message,
    )


def _existing_snooze_result(delivery: Delivery, task: Task, clock: Clock) -> SnoozeResult:
    if delivery.status == "sent":
        return SnoozeResult("sent", "Это отложенное напоминание уже отправлено.", delivery.id)
    return SnoozeResult(
        "already",
        render_snooze_confirmation(task, clock.tz, delivery.scheduled_for, already=True),
        delivery.id,
    )


def _regular_fire_at(due: datetime, lead_minutes: int, now: datetime) -> datetime | None:
    due = _floor(due)
    now = _floor(now)
    if due <= now:
        return None
    fire = due - timedelta(minutes=lead_minutes)
    if fire < now:
        return now
    return fire


def _digest_phase(now: datetime, digest_at: datetime) -> str:
    if now < digest_at:
        return "wait"
    if now <= digest_at + DIGEST_GRACE:
        return "due"
    return "late"


def _task_reason(task: Task | None) -> str:
    if task is None:
        return "task_deleted"
    if task.status != "open":
        return "task_closed"
    return "rescheduled"


def _floor(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    current = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0)


def _same_moment(left: datetime, right: datetime) -> bool:
    return dump_dt(_floor(left)) == dump_dt(_floor(right))
