"""Design tokens: both palettes, their contrast, and the saved preference.

Nothing here opens a window: the palettes are data and the preference is one
JSON file. The contrast rules are the accessibility contract of DESIGN.md 9:
every text/surface pair reads at 4.5:1, and the verdict chips (text on their own
tint) too.
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import theme

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI = Path(ROOT, "gui.py")

MODES = ("dark", "light")
SURFACES = ("bg.canvas", "bg.surface", "bg.raised", "bg.hover")
TEXT_TOKENS = ("text.primary", "text.secondary", "text.tertiary")
TONE_TOKENS = ("ok", "warn", "bad", "unknown", "danger")


def luminance(value):
    channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045
              else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground, background):
    first, second = luminance(foreground), luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


class TestPalette(unittest.TestCase):

    def test_both_modes_define_the_same_tokens(self):
        self.assertEqual(set(theme.DARK), set(theme.LIGHT),
                         "a token present in one palette and missing from the "
                         "other raises KeyError when that theme is selected")

    def test_every_token_resolves_in_both_modes(self):
        for mode in MODES:
            for token in theme.DARK:
                with self.subTest(mode=mode, token=token):
                    self.assertRegex(theme.color(token, mode), r"^#[0-9A-F]{6}$")

    def test_text_is_readable_on_every_surface(self):
        for mode in MODES:
            for text in TEXT_TOKENS:
                for surface in SURFACES:
                    with self.subTest(mode=mode, text=text, surface=surface):
                        self.assertGreaterEqual(
                            contrast(theme.color(text, mode),
                                     theme.color(surface, mode)), 4.5,
                            f"{text} on {surface} does not reach 4.5:1 in {mode}")

    def test_verdict_text_is_readable_on_its_chip_and_on_surfaces(self):
        for mode in MODES:
            for tone in TONE_TOKENS:
                backgrounds = [f"{tone}.bg", "bg.surface", "bg.raised",
                               "bg.canvas"]
                for background in backgrounds:
                    with self.subTest(mode=mode, tone=tone, background=background):
                        self.assertGreaterEqual(
                            contrast(theme.color(tone, mode),
                                     theme.color(background, mode)), 4.5,
                            f"{tone} on {background} does not reach 4.5:1 in {mode}")

    def test_the_primary_button_text_is_readable(self):
        for mode in MODES:
            for fill in ("accent", "accent.hover", "accent.pressed"):
                with self.subTest(mode=mode, fill=fill):
                    self.assertGreaterEqual(
                        contrast(theme.color("on.accent", mode),
                                 theme.color(fill, mode)), 4.5,
                        f"on.accent on {fill} does not reach 4.5:1 in {mode}")

    def test_the_light_theme_is_not_the_dark_one(self):
        for token in ("bg.canvas", "bg.surface", "text.primary", "accent"):
            with self.subTest(token=token):
                self.assertNotEqual(theme.color(token, "dark"),
                                    theme.color(token, "light"))


class TestPreference(unittest.TestCase):
    """`settings.json` next to `%APPDATA%`: read, written, and never fatal."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="flac_settings_")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.addCleanup(setattr, theme, "MODE", "dark")
        patcher = mock.patch.dict(os.environ, {"APPDATA": self.folder})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, text):
        path = theme.settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")

    def test_defaults_to_dark_when_the_file_is_missing(self):
        self.assertEqual(theme.load_preference(), "dark")
        self.assertEqual(theme.MODE, "dark")

    def test_saves_and_loads_the_light_theme(self):
        self.assertTrue(theme.save_preference("light"))
        path = theme.settings_path()
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.abspath(path).startswith(self.folder))
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {"theme": "light"})
        self.assertEqual(theme.load_preference(), "light")
        self.assertEqual(theme.MODE, "light")

    def test_malformed_json_falls_back_to_dark(self):
        self._write("{this is not json")
        self.assertEqual(theme.load_preference(), "dark")
        self.assertEqual(theme.MODE, "dark")

    def test_unknown_value_falls_back_to_dark(self):
        self._write('{"theme": "purple"}')
        self.assertEqual(theme.load_preference(), "dark")

    def test_wrong_shape_falls_back_to_dark(self):
        self._write('["light"]')
        self.assertEqual(theme.load_preference(), "dark")

    def test_unwritable_folder_does_not_raise(self):
        blocker = os.path.join(self.folder, "blocked")
        Path(blocker).write_text("not a folder", encoding="utf-8")
        with mock.patch.dict(os.environ, {"APPDATA": blocker}):
            self.assertFalse(theme.save_preference("light"))
            self.assertEqual(theme.load_preference(), "dark")

    def test_invalid_mode_is_rejected(self):
        self.assertFalse(theme.save_preference("purple"))
        self.assertFalse(os.path.exists(theme.settings_path()))


class TestHardcodedColours(unittest.TestCase):

    def test_gui_has_no_hardcoded_hex_colours(self):
        """Every colour comes from theme.py; a literal in gui.py is a bug."""
        source = GUI.read_text(encoding="utf-8")
        found = re.findall(r"#[0-9A-Fa-f]{6}", source)
        self.assertEqual(found, [], f"hard-coded colours in gui.py: {found}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
