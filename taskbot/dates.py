from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)


class Clock:
    """Текущий момент в часовом поясе пользователя, а не компьютера."""

    def __init__(self, tz: ZoneInfo, *, now: datetime | None = None) -> None:
        self.tz = tz
        self._frozen = now

    def now(self) -> datetime:
        if self._frozen is not None:
            return self._frozen.astimezone(self.tz)
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def now_utc(self) -> datetime:
        return self.now().astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(day: date, clock_time: time, tz: ZoneInfo) -> datetime:
    local = datetime(
        day.year,
        day.month,
        day.day,
        clock_time.hour,
        clock_time.minute,
        tzinfo=tz,
    )
    return local.astimezone(timezone.utc)


def from_utc(moment: datetime, tz: ZoneInfo) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz)


def dump_dt(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def load_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_overdue(
    task_date: date,
    due_at_utc: datetime | None,
    status: str,
    *,
    today: date,
    now_utc: datetime,
) -> bool:
    """Задача без времени просрочена только со следующего местного дня.

    Задача со временем просрочена после этого момента. В саму минуту срока
    она ещё не считается просроченной.
    """

    if status != "open":
        return False
    if due_at_utc is not None:
        due = due_at_utc
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        current = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc) > due.astimezone(timezone.utc)
    return task_date < today


def shift_day(day: date, days: int) -> date:
    return day + timedelta(days=days)
