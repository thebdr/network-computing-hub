"""The filter box's query language: rcm.parse_query and Query.matches.

Run from the repository root:  python -m unittest discover -s tests
Needs only the standard library: bin/rcm imports no GTK at the top.
"""
import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))
_loader = importlib.machinery.SourceFileLoader("rcm", str(BIN / "rcm"))
_spec = importlib.util.spec_from_loader("rcm", _loader)
rcm = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("rcm", rcm)
_loader.exec_module(rcm)


def conn(sel, host="", user="", tags=""):
    group, _, name = sel.rpartition("/")
    extra = {"rcm-tags": tags} if tags else {}
    return rcm.Conn(group, name, Path(f"/x/{sel}.rdp"), host=host,
                    username=user, extra=extra)


CONNS = [
    conn("FVT/SAFETY/sOPB", "192.168.100.41", "", "safety"),
    conn("FVT/SAFETY/sPLC", "192.168.100.40", "", "plc, safety"),
    conn("FVT/TRIB/bSorter", "192.168.100.10", "Production"),
    conn("FVT/TRIB/bSorterMaint", "192.168.100.74", "Production", "maint"),
    conn("FVT/TRIH/hSorter", "192.168.100.20", "Production"),
    conn("FVT/TRIH/hSorterMaint", "192.168.100.75", "", "maint"),
    conn("Lab/Café Box", "cafe.local", "bob", "plc2"),
]


def names(text):
    query = rcm.parse_query(text)
    return [c.name for c in CONNS if query.matches(c)]


ALL = [c.name for c in CONNS]


class PlainText(unittest.TestCase):
    def test_empty_and_blank_match_everything(self):
        self.assertEqual(names(""), ALL)
        self.assertEqual(names("   "), ALL)

    def test_a_word_is_found_anywhere_ignoring_case(self):
        self.assertEqual(names("SORTER"),
                         ["bSorter", "bSorterMaint", "hSorter", "hSorterMaint"])

    def test_free_text_reaches_path_host_user_and_tags(self):
        self.assertEqual(names("trib/"), ["bSorter", "bSorterMaint"])
        self.assertEqual(names("100\\.7"), ["bSorterMaint", "hSorterMaint"])
        self.assertEqual(names("^bob$"), ["Café Box"])
        self.assertEqual(names("^plc$"), ["sPLC"])

    def test_spaces_inside_a_regex_count(self):
        self.assertEqual(names("café box"), ["Café Box"])
        self.assertEqual(names("trib sorter"), [])

    def test_a_hash_inside_a_word_is_not_a_key(self):
        self.assertEqual(rcm.parse_query("a#b").problems, [])
        self.assertEqual(names("a#b"), [])


class Fields(unittest.TestCase):
    def test_each_field_key_looks_only_at_its_field(self):
        self.assertEqual(names("#name sorter$"), ["bSorter", "hSorter"])
        self.assertEqual(names("#host \\.7[45]$"), ["bSorterMaint", "hSorterMaint"])
        self.assertEqual(names("#group ^FVT/TRIH$"), ["hSorter", "hSorterMaint"])
        self.assertEqual(names("#user production"),
                         ["bSorter", "bSorterMaint", "hSorter"])
        self.assertEqual(names("#name trib"), [])      # trib is in the group

    def test_tag_is_tried_one_tag_at_a_time(self):
        self.assertEqual(names("#tag ^plc$"), ["sPLC"])
        self.assertEqual(names("#tag plc"), ["sPLC", "Café Box"])
        self.assertEqual(names("#tag ^safety$"), ["sOPB", "sPLC"])

    def test_empty_values_can_be_asked_for(self):
        self.assertEqual(names("#user ^$"), ["sOPB", "sPLC", "hSorterMaint"])

    def test_keys_ignore_case(self):
        self.assertEqual(names("#NAME sorter$"), ["bSorter", "hSorter"])


class Logic(unittest.TestCase):
    def test_terms_side_by_side_must_all_hold(self):
        self.assertEqual(names("#group trib #name sorter"),
                         ["bSorter", "bSorterMaint"])
        self.assertEqual(names("#group trib #and #name maint"), ["bSorterMaint"])

    def test_not_negates_the_next_term(self):
        self.assertEqual(names("#name sorter #not #name maint"),
                         ["bSorter", "hSorter"])
        self.assertEqual(names("#name sorter #not maint"), ["bSorter", "hSorter"])
        self.assertEqual(names("#not #tag ."), ["bSorter", "hSorter"])
        self.assertEqual(names("#not #not #name sorter$"), ["bSorter", "hSorter"])

    def test_or_gives_alternatives(self):
        self.assertEqual(names("#host \\.10$ #or #host \\.20$"),
                         ["bSorter", "hSorter"])
        self.assertEqual(names("#name sopb #or bob"), ["sOPB", "Café Box"])

    def test_and_binds_tighter_than_or(self):
        # sOPB, or (TRIH and maint)
        self.assertEqual(names("#name sopb #or #group trih #and #name maint"),
                         ["sOPB", "hSorterMaint"])

    def test_text_before_the_first_key_is_a_term_too(self):
        self.assertEqual(names("sorter #group trih"), ["hSorter", "hSorterMaint"])


class HalfTyped(unittest.TestCase):
    def test_a_key_with_nothing_after_it_adds_nothing(self):
        for text in ("#name", "#name ", "#host   ", "sorter #group"):
            query = rcm.parse_query(text)
            self.assertEqual(query.problems, [], text)
        self.assertEqual(names("#name "), ALL)
        self.assertEqual(names("sorter #group"),
                         ["bSorter", "bSorterMaint", "hSorter", "hSorterMaint"])

    def test_a_partial_key_at_the_end_is_still_being_typed(self):
        self.assertEqual(rcm.parse_query("sorter #ho").problems, [])
        self.assertEqual(names("sorter #ho"),
                         ["bSorter", "bSorterMaint", "hSorter", "hSorterMaint"])

    def test_trailing_operators_add_nothing(self):
        for text in ("#name sorter$ #or", "#name sorter$ #and",
                     "#name sorter$ #not"):
            self.assertEqual(names(text), ["bSorter", "hSorter"], text)
        self.assertEqual(names("#or #name sorter$"), ["bSorter", "hSorter"])


class Mistakes(unittest.TestCase):
    def test_a_broken_regex_is_matched_as_plain_text_and_said(self):
        query = rcm.parse_query("#name sort(")
        self.assertEqual([c.name for c in CONNS if query.matches(c)], [])
        self.assertEqual(len(query.problems), 1)
        self.assertIn("'sort(' is not a regex", query.problems[0])
        self.assertIn("matched as plain text", query.problems[0])
        self.assertEqual(names("café (box"), [])
        lab = conn("Lab/odd (name)")
        self.assertTrue(rcm.parse_query("#name odd (").matches(lab))

    def test_an_unknown_key_matches_nothing_and_names_the_keys(self):
        query = rcm.parse_query("#plc x")
        self.assertEqual([c.name for c in CONNS if query.matches(c)], [])
        self.assertEqual(query.problems, [
            "unknown key #plc -- the keys are #group #name #host #user #tag "
            "#and #or #not"])
        # a finished unknown key, even with nothing after it
        self.assertEqual(names("#plc sorter #name x"), [])
        self.assertTrue(rcm.parse_query("#hostname").problems)


class Highlighting(unittest.TestCase):
    def test_patterns_for_a_field_are_its_own_and_the_free_ones(self):
        query = rcm.parse_query("box #name sorter #not #name maint #host 10")
        self.assertEqual([p.pattern for p in query.patterns_for("name")],
                         ["box", "sorter"])
        self.assertEqual([p.pattern for p in query.patterns_for("host")],
                         ["box", "10"])


if __name__ == "__main__":
    unittest.main()
