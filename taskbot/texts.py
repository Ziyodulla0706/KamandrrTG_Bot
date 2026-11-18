BTN_ADD = "➕ Добавить задачу"
BTN_TODAY = "📅 Сегодня"
BTN_TOMORROW = "🌅 Завтра"
BTN_ALL = "📋 Все задачи"
BTN_DONE = "✅ Выполненные"
BTN_SETTINGS = "⚙️ Настройки"
BTN_NOTES = "📝 Заметки"
BTN_SEARCH = "🔎 Поиск"
BTN_WEEK = "📅 Неделя"
BTN_RECUR = "🔁 Повторы"
BTN_CATS = "🏷 Категории"
BTN_EXPORT = "📤 Экспорт"

MENU_BUTTONS = (
    BTN_ADD,
    BTN_NOTES,
    BTN_TODAY,
    BTN_TOMORROW,
    BTN_WEEK,
    BTN_ALL,
    BTN_DONE,
    BTN_SEARCH,
    BTN_RECUR,
    BTN_CATS,
    BTN_EXPORT,
    BTN_SETTINGS,
)
# Эти кнопки открывают разделы. Их обрабатывает самый первый роутер,
# поэтому нажатие никогда не становится текстом заметки, поиска или даты.
SECTION_BUTTONS = (
    BTN_NOTES,
    BTN_SEARCH,
    BTN_WEEK,
    BTN_RECUR,
    BTN_CATS,
    BTN_EXPORT,
)

SCOPE_TODAY = "td"
SCOPE_TOMORROW = "tm"
SCOPE_ALL = "al"
SCOPE_DONE = "dn"
SCOPE_WEEK = "wk"
SCOPE_SEARCH = "sr"

MENU_TO_SCOPE = {
    BTN_TODAY: SCOPE_TODAY,
    BTN_TOMORROW: SCOPE_TOMORROW,
    BTN_ALL: SCOPE_ALL,
    BTN_DONE: SCOPE_DONE,
}

EXAMPLES = (
    "• Завтра в 15:00 отправить КП поставщику\n"
    "• Сегодня проверить договор\n"
    "• 20.11.2026 в 10:30 позвонить подрядчику\n"
    "• 20.11 купить бумагу"
)

START = (
    "Привет! Я помогу вести личные задачи.\n\n"
    "Отправьте задачу сообщением. Одна строка — одна задача.\n\n"
    "Примеры:\n"
    f"{EXAMPLES}\n\n"
    "Понимаю «сегодня», «завтра», «послезавтра», дату ДД.ММ.ГГГГ или ДД.ММ "
    "и время ЧЧ:ММ.\n"
    "Если даты нет — спрошу. «В пятницу» и «на следующей неделе» не разбираю: "
    "нужно выбрать точный день.\n\n"
    "Остановить текущий диалог: /cancel или кнопка «Отмена».\n"
    "Заметки, поиск, неделя, повторы и экспорт — в меню внизу."
)

ADD_HELP = (
    "Напишите задачу одним сообщением. Каждая непустая строка — отдельная задача.\n\n"
    "Примеры:\n"
    f"{EXAMPLES}"
)

DATE_HINT = "Отправьте дату: 20.11.2026, 20.11, сегодня, завтра или послезавтра."
TIME_HINT = "Отправьте время в виде ЧЧ:ММ, например 15:00. Предлог «в» можно оставить."
STALE = "Это действие уже неактуально."
NOT_FOUND = "Задача не найдена или уже удалена."
STOPPED = "Диалог остановлен."
