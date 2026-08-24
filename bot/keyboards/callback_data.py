from aiogram.filters.callback_data import CallbackData


class MenuCB(CallbackData, prefix="menu"):
    """Payload for the main menu's inline buttons.

    A typed factory rather than bare `callback_data` strings: `pack()` produces
    `menu:<action>:<page>`, and `MenuCB.filter(F.action == "premium")` matches
    without string parsing in the handler.
    """

    action: str
    page: int = 0
