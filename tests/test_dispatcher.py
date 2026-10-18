import unittest

from palesql.commands.dispatcher import ALIASES, CommandDispatcher, CommandSpec, command_for
from palesql.state import PaleState


class CommandDispatcherTests(unittest.TestCase):
    def test_registered_command_receives_state_and_argument(self):
        received = []

        def command(state, argument):
            received.append((state, argument))
            return True

        state = PaleState()
        dispatcher = CommandDispatcher({".tables": command})

        self.assertTrue(dispatcher.dispatch(".tables", state, "users"))
        self.assertEqual(received, [(state, "users")])

    def test_group_handler_is_adapted_to_the_command_interface(self):
        received = []

        def grouped(name, argument, state):
            received.append((name, state, argument))
            return True

        state = PaleState()
        dispatcher = CommandDispatcher({".tables": command_for(".tables", grouped)})

        self.assertTrue(dispatcher.dispatch(".tables", state, "users"))
        self.assertEqual(received, [(".tables", state, "users")])

    def test_unknown_command_is_not_handled(self):
        self.assertFalse(CommandDispatcher().dispatch(".missing", PaleState(), ""))

    def test_alias_uses_the_canonical_handler(self):
        received = []

        def command(state, argument):
            received.append((state.command_name, argument))
            return True

        dispatcher = CommandDispatcher({".exit": command})
        self.assertNotIn(".quit", dispatcher.commands)
        self.assertEqual(ALIASES[".quit"], ".exit")
        self.assertTrue(dispatcher.dispatch(".quit", PaleState(), ""))
        self.assertEqual(received, [(".quit", "")])

    def test_command_metadata_is_kept_with_the_handler(self):
        dispatcher = CommandDispatcher({
            ".backup": CommandSpec(lambda state, argument: True, requires_connection=True, dangerous=True, files=True),
        })

        spec = dispatcher.spec(".backup")
        self.assertIsNotNone(spec)
        self.assertTrue(spec.requires_connection)
        self.assertTrue(spec.dangerous)
        self.assertTrue(spec.files)


if __name__ == "__main__":
    unittest.main()
