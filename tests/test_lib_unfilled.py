"""The question side (scripts/lib.py) and the grading API (api/grade.py) must agree on what
"unfilled" means (設計書 v4 §3.6.7, §5.2 A9〜A9d). No network; only invented cards, plus a
read-only pass over the real cards that prints file names only."""
import json
import os
import re
import unittest
from datetime import date
from unittest import mock

import card_fixtures as F
import grade
import kindle_update
import lib

FILL_HISTORY_RE = re.compile(r"^- \d{4}-\d{2}-\d{2} \| 補完 \|", re.M)


def no_network(*a, **k):
    raise AssertionError("urlopen must not be called in tests")


def filled_mark(card):
    """補完した印：回答履歴に「補完」の行がある（Web の補完）、または status が未履修でない（Mac の /recall）。"""
    return bool(FILL_HISTORY_RE.search(card["body"])) or card["fm"].get("status", "未履修") != "未履修"


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
        """実カードで、①grade と lib の判定が一致し、②独立した期待値（tests/expected_unfilled.json。
        判定を書き換える前の 04713de の lib.py で作った、ファイル名と真偽値だけの一覧）とも食い違わない。

        期待値はカードの補完・追加で古くなるので、正当な変化では落ちないようにしている：
        - 期待値にないカード（新しく作ったカード）は②の対象外。期待値にあって今ないカードも対象外
        - 期待値で未記入（true）→ 今は記入済み、は「補完した印」（回答履歴に「補完」の行がある、または
          status が未履修でない。Mac の /recall で手で補完したときは履歴が付かず status: 要復習 になる）があれば可
        - 期待値で記入済み（false）→ 今は未記入、は常に失敗（記入済みのカードが未記入に戻ることはない）
        新しい状態で固定し直すとき：cd 90.mybrain/cards && python3 tests/update_expected_unfilled.py"""
        cards = lib.load_all_cards(F.CARDS_DIR)
        self.assertGreater(len(cards), 0)
        mismatched = [os.path.basename(c["path"]) for c in cards
                      if grade.pending_sections(c["body"]) != lib.pending_sections(c["body"])]
        self.assertEqual(mismatched, [], "grade と lib の判定が違うカード（ファイル名だけ）")

        with open(os.path.join(F.HERE, "expected_unfilled.json"), encoding="utf-8") as f:
            expected = json.load(f)["cards"]
        compared, wrongly_filled, wrongly_unfilled = 0, [], []
        for c in cards:
            name = os.path.basename(c["path"])
            if name not in expected:
                continue
            compared += 1
            actual = lib.is_unfilled(c)
            if expected[name] and not actual and not filled_mark(c):
                wrongly_filled.append(name)
            elif not expected[name] and actual:
                wrongly_unfilled.append(name)
        self.assertGreater(compared, 0, "期待値と照合できたカードがない")
        self.assertEqual(wrongly_filled, [], "補完した印がないのに記入済みと判定されたカード（ファイル名だけ）")
        self.assertEqual(wrongly_unfilled, [], "記入済みだったのに未記入と判定されたカード（ファイル名だけ）")

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
