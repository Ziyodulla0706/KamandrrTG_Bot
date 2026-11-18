from aiogram import Router

from taskbot.handlers import (
    card,
    categories,
    export,
    fallback,
    intake,
    menu,
    notes,
    recur,
    search,
    settings,
    snooze,
)


def setup_router() -> Router:
    root = Router(name="root")
    # menu первым: кнопки разделов и /cancel выигрывают у любого ожидания ввода.
    root.include_router(menu.router)
    root.include_router(settings.router)
    root.include_router(snooze.router)
    root.include_router(notes.router)
    root.include_router(search.router)
    root.include_router(categories.router)
    root.include_router(recur.router)
    root.include_router(export.router)
    root.include_router(intake.router)
    root.include_router(card.router)
    root.include_router(fallback.router)
    return root
