"""A screen offset must not be presented as a screenshot scale factor."""

import unittest

from tools.computer_use.backend import UIElement
from tools.computer_use.tool import _bounds_hints


class CoordinateSpaceHintTests(unittest.TestCase):
    def test_offset_window_does_not_turn_desktop_bounds_into_click_pixels(self):
        elements = [UIElement(3, "Document", "Hermes", (1327, 0, 1584, 2111))]
        scale, note = _bounds_hints(elements, 1093, 1456)
        self.assertIsNone(scale)
        self.assertIn("window-local screenshot", note)
        self.assertNotIn("clicks expect the native", note)

    def test_scaled_window_requires_the_driver_coordinate_contract(self):
        scale, note = _bounds_hints([UIElement(0, "Window", "", (0, 0, 1600, 1200))], 800, 600)
        self.assertIsNone(scale)
        self.assertIn("do not pass native desktop bounds", note)

    def test_matching_geometry_does_not_add_a_conversion_hint(self):
        self.assertEqual(_bounds_hints([UIElement(0, "Window", "", (0, 0, 800, 600))], 800, 600), (None, None))


if __name__ == "__main__":
    unittest.main()
