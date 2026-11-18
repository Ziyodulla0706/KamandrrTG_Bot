from aiogram.fsm.state import State, StatesGroup


class Clarify(StatesGroup):
    choose_date = State()
    enter_date = State()
    confirm_past = State()


class Reschedule(StatesGroup):
    choose_day = State()
    enter_date = State()
    confirm_past = State()
    choose_time = State()
    enter_time = State()


class Rename(StatesGroup):
    enter_title = State()


class SettingsDialog(StatesGroup):
    enter_time = State()


class NoteDialog(StatesGroup):
    add = State()
    edit = State()
    task_title = State()


class SearchDialog(StatesGroup):
    query = State()


class CatDialog(StatesGroup):
    create = State()
    rename = State()


class RecurDialog(StatesGroup):
    title = State()
    kind = State()
    weekdays = State()
    month_day = State()
    start = State()
    start_text = State()
    time = State()
    category = State()
    confirm = State()


class RecurEdit(StatesGroup):
    title = State()
    kind = State()
    weekdays = State()
    month_day = State()
    time = State()
    confirm = State()
