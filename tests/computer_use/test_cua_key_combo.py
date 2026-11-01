"""Key chords must retain the modifiers that the input guard recognizes."""

import unittest

from tools.computer_use.cua_backend_parse import _parse_key_combo


class KeyComboTests(unittest.TestCase):
    def test_windows_run_chord_keeps_its_modifier(self):
        for modifier in ("win", "windows", "super", "meta"):
            with self.subTest(modifier=modifier):
                self.assertEqual(_parse_key_combo(f"{modifier}+r"), ("r", ["win"]))

    def test_windows_modifier_composes_with_other_modifiers(self):
        self.assertEqual(_parse_key_combo("ctrl-windows-shift-s"), ("s", ["ctrl", "win", "shift"]))

    def test_existing_chords_and_plain_keys_keep_their_meaning(self):
        for chord, expected in (
            ("cmd+s", ("s", ["cmd"])),
            ("control-alt-t", ("t", ["ctrl", "option"])),
            ("enter", ("enter", [])),
        ):
            with self.subTest(chord=chord):
                self.assertEqual(_parse_key_combo(chord), expected)


if __name__ == "__main__":
    unittest.main()
