"""ui.conf: the manager's window state, as load_ui_state and save_ui_state see it.

Run from the repository root:  python -m unittest discover -s tests
"""
import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))
if "rcm" in sys.modules:
    rcm = sys.modules["rcm"]
else:
    _loader = importlib.machinery.SourceFileLoader("rcm", str(BIN / "rcm"))
    _spec = importlib.util.spec_from_loader("rcm", _loader)
    rcm = importlib.util.module_from_spec(_spec)
    sys.modules["rcm"] = rcm
    _loader.exec_module(rcm)


class DarkMode(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.saved = rcm.UI_CONF
        rcm.UI_CONF = Path(self.folder.name) / "ui.conf"

    def tearDown(self):
        rcm.UI_CONF = self.saved
        self.folder.cleanup()

    def write(self, text):
        rcm.UI_CONF.write_text(text, encoding="utf-8")

    def test_no_file_means_light(self):
        self.assertIs(rcm.load_ui_state()["dark"], False)

    def test_a_file_without_the_key_means_light(self):
        self.write("[ui]\nlayout = browse\n")
        self.assertIs(rcm.load_ui_state()["dark"], False)

    def test_on_and_off_read_back(self):
        self.write("[ui]\ndark = on\n")
        self.assertIs(rcm.load_ui_state()["dark"], True)
        self.write("[ui]\ndark = off\n")
        self.assertIs(rcm.load_ui_state()["dark"], False)

    def test_a_value_that_is_not_a_boolean_means_light(self):
        self.write("[ui]\ndark = midnight\n")
        self.assertIs(rcm.load_ui_state()["dark"], False)

    def test_save_writes_it_and_keeps_the_rest(self):
        state = rcm.load_ui_state()
        state.update(dark=True, layout="classic", browse_view="icons",
                     tabs=[("sorters", "", "#name sorter")])
        rcm.save_ui_state(state)
        text = rcm.UI_CONF.read_text(encoding="utf-8")
        self.assertIn("\ndark = on\n", text)
        again = rcm.load_ui_state()
        self.assertEqual((again["dark"], again["layout"], again["browse_view"],
                          again["tabs"]),
                         (True, "classic", "icons",
                          [("sorters", "", "#name sorter")]))

    def test_the_documented_default_says_off(self):
        self.write(rcm.DEFAULT_CONFIGS["ui.conf"])
        self.assertIs(rcm.load_ui_state()["dark"], False)
        self.assertIn("\ndark = off\n", rcm.DEFAULT_CONFIGS["ui.conf"])


if __name__ == "__main__":
    unittest.main()
