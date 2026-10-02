"""The question side (scripts/lib.py) and the grading API (api/grade.py) must agree on what
"unfilled" means (設計書 v4 §3.6.7, §5.2 A9〜A9d). No network; only invented cards, plus a
read-only pass over the real cards that prints file names only."""
import unittest
from datetime import date
from unittest import mock

import card_fixtures as F
import grade
import kindle_update
import lib


def no_network(*a, **k):
    raise AssertionError("urlopen must not be called in tests")


class LibUnfilledTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(grade.urllib.request, "urlopen", no_network)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_A9_fixtures_agree(self):
        bodies = {k: F.body_of(v) for k, v in F.ALL.items()}
        bodies.update({"D2 " + k: F.body_of(v) for k, v in F.d2_bodies().items()})
        for key, body in bodies.items():
            with self.subTest(card=key):
                self.assertEqual(grade.pending_sections(body), lib.pending_sections(body))
                self.assertEqual(grade.is_unfilled(body), lib.is_unfilled({"body": body}))
        for key, text in F.ALL.items():
            with self.subTest(expected=key):
                self.assertEqual(lib.pending_sections(F.body_of(text)), F.EXPECTED_PENDING[key])

    def test_A9b_real_cards_agree(self):
        cards = lib.load_all_cards(F.CARDS_DIR)
        self.assertGreater(len(cards), 0)
        mismatched = [c["path"].rsplit("/", 1)[-1] for c in cards
                      if grade.pending_sections(c["body"]) != lib.pending_sections(c["body"])]
        self.assertEqual(mismatched, [], "grade と lib の判定が違うカード（ファイル名だけ）")

    def test_A9c_template_is_unfilled(self):
        h = {"loc": "10", "url": "https://example.com/10", "memo": ""}
        text = kindle_update.render_card("架空のカード", "架空の本", ["テスト"], "架空の主張の下書き。",
                                         "架空の引用文。", h, "架空の本.md", "2026-10-02")
        body = F.body_of(text)
        self.assertEqual(grade.pending_sections(body), {"claim", "why", "scene"})
        self.assertEqual(lib.pending_sections(body), {"claim", "why", "scene"})

    def test_A9d_marks_are_the_same(self):
        self.assertEqual(grade.OLD_SCENE_LINES, F.OLD_LINES)
        self.assertEqual(lib.OLD_SCENE_LINES, F.OLD_LINES)
        for name, value in (("CLAIM_MARK", F.CLAIM_MARK), ("WHY_MARK", F.WHY_MARK), ("SCENE_MARK", F.SCENE_MARK),
                            ("OLD_SCENE_SUFFIX", F.OLD_SUFFIX)):
            with self.subTest(name=name):
                self.assertEqual(getattr(grade, name), value)
                self.assertEqual(getattr(lib, name), value)
        self.assertEqual(grade.FIELD_SECTION, lib.FIELD_SECTION)
        self.assertEqual(grade.OLD_SCENE_CATEGORIES, lib.OLD_SCENE_CATEGORIES)

    def test_select_cards_treats_filled_cards_as_graded(self):
        """(c*)・(c†) は補完でなく期限到来の採点の問い、(b) は補完の問いとして選ばれる。"""
        b = F.parse(F.make_card(book="本B"), "b.md")
        c_star = F.parse(F.C_STAR.replace("架空の本", "本Cstar"), "c_star.md")
        c_dagger = F.parse(F.C_DAGGER.replace("架空の本", "本Cdagger").replace("next_review: 2026-10-03",
                                                                           "next_review: 2026-10-02"), "c_dagger.md")
        with mock.patch.object(lib, "today_jst", lambda: date(2026, 10, 2)), \
                mock.patch.object(lib, "MAX_FILL_PER_DAY", 1):
            picked = lib.select_cards([b, c_star, c_dagger], limit=3)
        got = {c["path"]: q for c, q in picked}
        self.assertEqual(got, {"b.md": "fill", "c_star.md": lib.qtype_for(c_star), "c_dagger.md": lib.qtype_for(c_dagger)})
        self.assertEqual(got["c_star.md"], "recall")
        others = lib.other_card_for_contrast(b, [b, c_star, c_dagger])
        self.assertIn(others["path"], ("c_star.md", "c_dagger.md"))


if __name__ == "__main__":
    unittest.main()
