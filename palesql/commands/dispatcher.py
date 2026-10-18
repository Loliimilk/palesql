from collections.abc import Callable
from typing import Any


class CommandDispatcher:
    def __init__(self, *handlers: Callable[[str, str, Any], bool]):
        self._handlers = list(handlers)

    def add(self, handler: Callable[[str, str, Any], bool]):
        self._handlers.append(handler)
        return handler

    def dispatch(self, command: str, argument: str, state: Any) -> bool:
        return any(handler(command, argument, state) for handler in self._handlers)
