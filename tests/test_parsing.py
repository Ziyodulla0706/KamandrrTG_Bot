from datetime import date, time

from taskbot.parsing import (
    AMBIGUOUS,
    CONFLICT,
    EMPTY_TITLE,
    INVALID_DATE,
    INVALID_TIME,
    NEEDS_DATE,
    OK,
    PAST,
    parse_exact_date,
    parse_line,
)


TODAY = date(2026, 11, 16)


def test_examples_from_the_spec():
    tomorrow = parse_line("Завтра в 15:00 отправить КП поставщику", today=TODAY)
    assert tomorrow.kind == OK
    assert tomorrow.task_date == date(2026, 11, 17)
    assert tomorrow.time_local == time(15, 0)
    assert tomorrow.title == "Отправить КП поставщику"

    today = parse_line("Сегодня проверить договор", today=TODAY)
    assert today.kind == OK
    assert today.task_date == TODAY
    assert today.time_local is None
    assert today.title == "Проверить договор"

    full = parse_line("20.11.2026 в 10:30 позвонить подрядчику", today=TODAY)
    assert full.kind == OK
    assert full.task_date == date(2026, 11, 20)
    assert full.time_local == time(10, 30)
    assert full.title == "Позвонить подрядчику"
    assert full.year_inferred is False


def test_date_without_year_uses_current_year_and_does_not_roll_forward():
    future = parse_line("20.11 купить бумагу", today=TODAY)
    assert future.kind == OK
    assert future.task_date == date(2026, 11, 20)
    assert future.year_inferred is True

    past = parse_line("20.01 купить бумагу", today=TODAY)
    assert past.kind == PAST
    assert past.task_date == date(2026, 1, 20)
    assert past.year_inferred is True

    new_year = parse_line("01.01 поздравить", today=date(2026, 12, 31))
    assert new_year.kind == PAST
    assert new_year.task_date == date(2026, 1, 1)


def test_tomorrow_uses_provided_local_today_not_utc_date():
    local_today = date(2026, 3, 21)
    parsed = parse_line("завтра отправить КП", today=local_today)
    assert parsed.task_date == date(2026, 3, 22)
    assert parsed.task_date != date(2026, 3, 21)


def test_invalid_and_ambiguous_dates_are_not_guessed():
    invalid = parse_line("31.02.2026 созвон", today=TODAY)
    assert invalid.kind == INVALID_DATE
    assert "31.02.2026" in invalid.message

    leap = parse_line("29.02.2025 отчёт", today=TODAY)
    assert leap.kind == INVALID_DATE

    valid_leap = parse_line("29.02.2024 отчёт", today=date(2024, 1, 1))
    assert valid_leap.kind == OK
    assert valid_leap.task_date == date(2024, 2, 29)

    short_year = parse_line("20.11.26 купить", today=TODAY)
    assert short_year.kind == INVALID_DATE

    friday = parse_line("в пятницу отправить отчёт", today=TODAY)
    assert friday.kind == AMBIGUOUS
    assert friday.title == "в пятницу отправить отчёт"
    assert friday.task_date is None

    week = parse_line("на следующей неделе отчёт", today=TODAY)
    assert week.kind == AMBIGUOUS

    mixed = parse_line("завтра в пятницу созвон", today=TODAY)
    assert mixed.kind == CONFLICT

    timed = parse_line("в пятницу в 15:00 созвон", today=TODAY)
    assert timed.kind == AMBIGUOUS
    assert timed.time_local == time(15, 0)
    assert "в пятницу" in timed.title


def test_unsupported_numbers_are_not_treated_as_dates():
    version = parse_line("проверить версию 2.5", today=TODAY)
    assert version.kind == NEEDS_DATE
    assert "2.5" in version.title

    decimal = parse_line("пункт 10.99", today=TODAY)
    assert decimal.kind == NEEDS_DATE
    assert "10.99" in decimal.title


def test_several_dates_or_times_are_rejected():
    times = parse_line("в 10:00 и в 12:00 позвонить", today=TODAY)
    assert times.kind == CONFLICT
    dates = parse_line("20.11.2026 или 21.11.2026 позвонить", today=TODAY)
    assert dates.kind == CONFLICT
    bad_time = parse_line("завтра в 25:00 позвонить", today=TODAY)
    assert bad_time.kind == INVALID_TIME


def test_missing_title_and_time_without_date():
    assert parse_line("завтра", today=TODAY).kind == EMPTY_TITLE
    only_time = parse_line("в 15:00", today=TODAY)
    assert only_time.kind == EMPTY_TITLE

    named = parse_line("в 09:05 позвонить", today=TODAY)
    assert named.kind == NEEDS_DATE
    assert named.time_local == time(9, 5)
    assert named.title == "Позвонить"
    assert named.task_date is None


def test_each_line_keeps_its_own_date():
    text = "завтра в 15:00 первое\nвторое без даты\n\n20.11.2026 третье"
    lines = [parse_line(line, today=TODAY) for line in text.splitlines() if line.strip()]
    assert lines[0].task_date == date(2026, 11, 17)
    assert lines[1].kind == NEEDS_DATE
    assert lines[1].task_date is None
    assert lines[2].task_date == date(2026, 11, 20)


def test_exact_date_entry():
    ok = parse_exact_date("20.11.2026", today=TODAY)
    assert ok.kind == OK
    assert ok.task_date == date(2026, 11, 20)
    past = parse_exact_date("20.01", today=TODAY)
    assert past.kind == PAST
    assert past.task_date == date(2026, 1, 20)
    assert past.year_inferred is True
    garbage = parse_exact_date("в пятницу", today=TODAY)
    assert garbage.kind == INVALID_DATE
