from collections.abc import Callable, Mapping
from dataclasses import dataclass
import sys
from typing import Any


Command = Callable[[Any, str], bool]
GroupedHandler = Callable[[str, str, Any], bool]

ALIASES = {
    ".quit": ".exit",
    ".system": ".shell",
    ".busy_timeout": ".timeout",
}


def command_for(name: str, handler: GroupedHandler, *args: Any, **kwargs: Any) -> Command:
    def command(state: Any, argument: str) -> bool:
        return handler(name, argument, state, *args, **kwargs)

    command.__name__ = f"command_{name.removeprefix('.').replace('-', '_')}"
    return command


@dataclass(frozen=True)
class CommandSpec:
    handler: Command
    requires_connection: bool = False
    dangerous: bool = False
    files: bool = False


class CommandDispatcher:
    def __init__(self, commands: Mapping[str, Command | CommandSpec] | None = None, aliases: Mapping[str, str] | None = None):
        self.commands = {
            name: command if isinstance(command, CommandSpec) else CommandSpec(command)
            for name, command in (commands or {}).items()
        }
        self.aliases = dict(ALIASES if aliases is None else aliases)

    def register(self, name: str, command: Command | CommandSpec) -> Command | CommandSpec:
        self.commands[name] = command if isinstance(command, CommandSpec) else CommandSpec(command)
        return command

    def resolve(self, name: str) -> str:
        return self.aliases.get(name, name)

    def spec(self, name: str) -> CommandSpec | None:
        return self.commands.get(self.resolve(name))

    def dispatch(self, name: str, state: Any, argument: str) -> bool:
        spec = self.spec(name)
        if spec is None:
            return False
        previous_name = getattr(state, "command_name", None)
        state.command_name = name
        try:
            if spec.dangerous and state.safe_mode:
                if state.safe_nonce is None:
                    print(f"Safe mode: {name} is disabled", file=sys.stderr)
                    state.failed = True
                    return True
                state.safe_nonce = None
            return bool(spec.handler(state, argument))
        finally:
            state.command_name = previous_name
