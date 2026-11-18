import re
from dataclasses import dataclass
from datetime import date, time, timedelta

OK = "ok"
NEEDS_DATE = "needs_date"
AMBIGUOUS = "ambiguous"
PAST = "past"
INVALID_DATE = "invalid_date"
INVALID_TIME = "invalid_time"
EMPTY_TITLE = "empty_title"
CONFLICT = "conflict"
TOO_LONG = "too_long"

QUEUE_KINDS = {NEEDS_DATE, AMBIGUOUS, PAST}
REJECTED_KINDS = {INVALID_DATE, INVALID_TIME, EMPTY_TITLE, CONFLICT, TOO_LONG}
MAX_LINE_LENGTH = 2000

_RELATIVE = re.compile(r"(?iu)(?<!\w)(сегодня|завтра|послезавтра)(?!\w)")
_YEAR_DATE = re.compile(r"(?<!\w)(\d{1,2})\.(\d{1,2})\.(\d+)(?!\w)")
_SHORT_DATE = re.compile(r"(?<!\w)(\d{1,2})\.(\d{2})(?!\w)")
_TIME = re.compile(r"(?iu)(?<!\w)(?:в\s*)?(\d{1,2}):(\d{2})(?!\d)")
_EXACT_DATE = re.compile(
    r"(?iu)^(сегодня|завтра|послезавтра|(\d{1,2})\.(\d{1,2})\.(\d{4})|(\d{1,2})\.(\d{2}))$"
)
_EXACT_TIME = re.compile(r"(?iu)^(?:в\s*)?(\d{1,2}):(\d{2})$")
_AMBIGUOUS = re.compile(
    r"(?iu)(?<!\w)("
    r"понедельника|понедельник|"
    r"вторника|вторник|"
    r"среду|среды|среда|"
    r"четверга|четверг|"
    r"пятницу|пятницы|пятница|"
    r"субботу|субботы|суббота|"
    r"воскресенья|воскресенье|"
    r"на\s+следующей\s+неделе|"
    r"на\s+этой\s+неделе|"
    r"следующей\s+неделе|"
    r"следующую\s+неделю|"
    r"через\s+неделю|"
    r"на\s+неделе|"
    r"через\s+(?:один\s+|одну\s+)?день|"
    r"через\s+\d+\s+(?:день|дня|дней)|"
    r"на\s+выходных|"
    r"в\s+выходные|"
    r"выходные"
    r")(?!\w)"
)
_EDGE_PUNCT = re.compile(r"(?:^[\s,.;:!?—–-]+)|(?:[\s,.;:!?—–-]+$)")


@dataclass(frozen=True)
class ParseResult:
    kind: str
    title: str
    source_text: str
    task_date: date | None = None
    time_local: time | None = None
    year_inferred: bool = False
    message: str = ""


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    kind: str
    value: object
    raw: str


def parse_line(text: str, *, today: date) -> ParseResult:
    source = " ".join(text.split())
    if len(source) > MAX_LINE_LENGTH:
        return ParseResult(
            kind=TOO_LONG,
            title=source,
            source_text=source,
            message="Строка длиннее 2000 символов. Сократите её и отправьте снова.",
        )

    times = _time_spans(source)
    bad_time = next((item for item in times if item.kind == "bad_time"), None)
    if bad_time is not None:
        return ParseResult(
            kind=INVALID_TIME,
            title=source,
            source_text=source,
            message=(
                f"Время {bad_time.raw} не существует. "
                "Используйте ЧЧ:ММ от 00:00 до 23:59."
            ),
        )
    if len(times) > 1:
        return ParseResult(
            kind=CONFLICT,
            title=source,
            source_text=source,
            message=(
                "В строке несколько значений времени. "
                "Оставьте одно время в формате ЧЧ:ММ и отправьте строку снова."
            ),
        )

    dates = _date_spans(source, today)
    bad_date = next((item for item in dates if item.kind == "bad_date"), None)
    if bad_date is not None:
        return ParseResult(
            kind=INVALID_DATE,
            title=source,
            source_text=source,
            message=str(bad_date.value),
        )
    if len(dates) > 1:
        return ParseResult(
            kind=CONFLICT,
            title=source,
            source_text=source,
            message=(
                "В строке несколько дат. Оставьте одну дату и отправьте строку снова."
            ),
        )

    ambiguous = _AMBIGUOUS.search(source) is not None
    if ambiguous and dates:
        return ParseResult(
            kind=CONFLICT,
            title=source,
            source_text=source,
            message=(
                "В строке одновременно точная дата и формулировка вроде «в пятницу» "
                "или «на следующей неделе». Оставьте что-то одно и отправьте строку снова."
            ),
        )

    time_local = times[0].value if times else None
    if time_local is not None and not isinstance(time_local, time):
        time_local = None

    if ambiguous:
        return ParseResult(
            kind=AMBIGUOUS,
            title=source,
            source_text=source,
            time_local=time_local,
            message="Нужна точная дата.",
        )

    if len(dates) == 1:
        span = dates[0]
        task_date, year_inferred = span.value  # type: ignore[misc]
        title = _title_without(source, [span, *times])
        if not title:
            return _empty_title(source)
        if task_date < today:
            return ParseResult(
                kind=PAST,
                title=title,
                source_text=source,
                task_date=task_date,
                time_local=time_local,
                year_inferred=year_inferred,
                message="Дата уже прошла.",
            )
        return ParseResult(
            kind=OK,
            title=title,
            source_text=source,
            task_date=task_date,
            time_local=time_local,
            year_inferred=year_inferred,
        )

    title = _title_without(source, times)
    if not title:
        return _empty_title(source)
    return ParseResult(
        kind=NEEDS_DATE,
        title=title,
        source_text=source,
        time_local=time_local,
        message="Дата не указана.",
    )


def parse_exact_date(text: str, *, today: date) -> ParseResult:
    source = " ".join(text.split())
    match = _EXACT_DATE.fullmatch(source)
    if match is None:
        return ParseResult(
            kind=INVALID_DATE,
            title=source,
            source_text=source,
            message=(
                "Не понял дату. Отправьте её так: 20.11.2026, 20.11, "
                "сегодня, завтра или послезавтра."
            ),
        )
    word = match.group(1).casefold()
    if word in {"сегодня", "завтра", "послезавтра"}:
        return ParseResult(
            kind=OK,
            title="",
            source_text=source,
            task_date=_relative_day(word, today),
        )
    if match.group(4):
        parsed = _calendar_span(
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            raw=source,
            year_inferred=False,
            start=0,
            end=len(source),
        )
    else:
        parsed = _calendar_span(
            int(match.group(5)),
            int(match.group(6)),
            today.year,
            raw=source,
            year_inferred=True,
            start=0,
            end=len(source),
        )
    if parsed.kind == "bad_date":
        return ParseResult(
            kind=INVALID_DATE,
            title=source,
            source_text=source,
            message=str(parsed.value),
        )
    task_date, year_inferred = parsed.value  # type: ignore[misc]
    kind = PAST if task_date < today else OK
    return ParseResult(
        kind=kind,
        title="",
        source_text=source,
        task_date=task_date,
        year_inferred=year_inferred,
        message="Дата уже прошла." if kind == PAST else "",
    )


def parse_exact_time(text: str) -> time | None:
    match = _EXACT_TIME.fullmatch(" ".join(text.split()))
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def payload_from_parsed(line_no: int, parsed: ParseResult) -> dict:
    return {
        "line": line_no,
        "source_text": parsed.source_text,
        "title": parsed.title,
        "time": parsed.time_local.strftime("%H:%M") if parsed.time_local else None,
        "kind": parsed.kind,
        "proposed_date": parsed.task_date.isoformat() if parsed.task_date else None,
        "year_inferred": parsed.year_inferred,
        "message": parsed.message,
    }


def _empty_title(source: str) -> ParseResult:
    return ParseResult(
        kind=EMPTY_TITLE,
        title="",
        source_text=source,
        message="Не вижу, что нужно сделать. Добавьте название задачи.",
    )


def _time_spans(source: str) -> list[_Span]:
    spans: list[_Span] = []
    for match in _TIME.finditer(source):
        hour = int(match.group(1))
        minute = int(match.group(2))
        shown = f"{hour:02d}:{minute:02d}"
        if hour > 23 or minute > 59:
            spans.append(_Span(match.start(), match.end(), "bad_time", shown, shown))
        else:
            spans.append(
                _Span(match.start(), match.end(), "time", time(hour, minute), match.group(0))
            )
    return spans


def _date_spans(source: str, today: date) -> list[_Span]:
    spans: list[_Span] = []
    occupied: list[tuple[int, int]] = []

    for match in _YEAR_DATE.finditer(source):
        raw = match.group(0)
        year_text = match.group(3)
        if len(year_text) != 4:
            spans.append(
                _Span(
                    match.start(),
                    match.end(),
                    "bad_date",
                    "Год в дате укажите четырьмя цифрами: ДД.ММ.ГГГГ.",
                    raw,
                )
            )
        else:
            spans.append(
                _calendar_span(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(year_text),
                    raw=raw,
                    year_inferred=False,
                    start=match.start(),
                    end=match.end(),
                )
            )
        occupied.append((match.start(), match.end()))

    for match in _SHORT_DATE.finditer(source):
        if _overlaps(match.start(), match.end(), occupied):
            continue
        day = int(match.group(1))
        month = int(match.group(2))
        # «10.99» и «2.5» не считаем датой. Неверный день при месяце 01–12 — ошибка.
        if month < 1 or month > 12:
            continue
        spans.append(
            _calendar_span(
                day,
                month,
                today.year,
                raw=match.group(0),
                year_inferred=True,
                start=match.start(),
                end=match.end(),
            )
        )
        occupied.append((match.start(), match.end()))

    for match in _RELATIVE.finditer(source):
        if _overlaps(match.start(), match.end(), occupied):
            continue
        word = match.group(1).casefold()
        spans.append(
            _Span(
                match.start(),
                match.end(),
                "date",
                (_relative_day(word, today), False),
                match.group(0),
            )
        )
    return spans


def _calendar_span(
    day: int,
    month: int,
    year: int,
    *,
    raw: str,
    year_inferred: bool,
    start: int,
    end: int,
) -> _Span:
    if month < 1 or month > 12 or day < 1 or day > 31:
        return _Span(start, end, "bad_date", f"Дата {raw} не существует.", raw)
    try:
        parsed = date(year, month, day)
    except ValueError:
        return _Span(start, end, "bad_date", f"Дата {raw} не существует.", raw)
    return _Span(start, end, "date", (parsed, year_inferred), raw)


def _relative_day(word: str, today: date) -> date:
    if word == "сегодня":
        return today
    if word == "завтра":
        return today + timedelta(days=1)
    if word == "послезавтра":
        return today + timedelta(days=2)
    raise ValueError(word)


def _overlaps(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(not (end <= left or start >= right) for left, right in occupied)


def _title_without(source: str, spans: list[_Span]) -> str:
    pieces: list[str] = []
    cursor = 0
    for span in sorted(spans, key=lambda item: item.start):
        pieces.append(source[cursor : span.start])
        cursor = span.end
    pieces.append(source[cursor:])
    title = " ".join("".join(pieces).split())
    title = _EDGE_PUNCT.sub("", title).strip()
    title = re.sub(r"(,\s*){2,}", ", ", title)
    if not title:
        return ""
    return title[:1].upper() + title[1:]
