"""スキップ（2026-11-15〜、99.ActionData のフロー .claude/flows/20261115_cards-skip.md）の試験。
api/grade.py の apply_skip・入力の検査、scripts/lib.py の is_skipped・select_cards、ページのボタン。架空のカードだけ・ネットワークなし。"""
import io
import json
import unittest
from datetime import date
from unittest import mock

import card_fixtures as F
import grade
import lib
import select_and_post

TODAY = date(2026, 11, 15)


class ApplySkip(unittest.TestCase):
    def test_skip_keeps_state_and_sets_next_review(self):
        text = F.make_card(claim="X", why="Y", scene="- Z", status="学習中", streak="2",
                           last_reviewed="2026-11-10", history=F.PASS_HIST)
        c = F.parse(text)
        new_text, resp = grade.apply_skip(c["fm"], c["body"], "架空の問い", TODAY)
        self.assertEqual(resp, {"ok": True, "type": "skip", "next_review": "2026-11-22"})
        n = F.parse(new_text)
        self.assertEqual(n["fm"]["status"], "学習中")
        self.assertEqual(n["fm"]["streak"], "2")
        self.assertEqual(n["fm"]["last_reviewed"], "2026-11-10")
        self.assertEqual(n["fm"]["next_review"], "2026-11-22")
        rows = grade.HIST_RE.findall(n["body"])
        self.assertEqual(rows[0], ("2026-11-15", "スキップ", "-", "スキップ"))
        self.assertIn("  - 問い: 架空の問い", n["body"])

    def test_skip_unfilled_card_stays_unfilled(self):
        c = F.parse(F.make_card())
        new_text, _ = grade.apply_skip(c["fm"], c["body"], "", TODAY)
        self.assertTrue(grade.is_unfilled(F.parse(new_text)["body"]))

    def test_skip_row_does_not_count_as_pass_or_graded(self):
        c = F.parse(F.make_card(claim="X", why="Y", scene="- Z"))
        new_text, _ = grade.apply_skip(c["fm"], c["body"], "", TODAY)
        n = F.parse(new_text)
        self.assertEqual(lib.parse_history(n), (0, 0))
        self.assertFalse(lib.last_failed(n))


class Validate(unittest.TestCase):
    def req(self, payload):
        raw = json.dumps(payload).encode("utf-8")
        return grade.read_and_validate(str(len(raw)), io.BytesIO(raw), TODAY)

    def test_skip_accepted_without_answers_text(self):
        r = self.req({"card": "架空.md", "qtype": "skip", "answers": {}})
        self.assertEqual(r.qtype, "skip")
        self.assertIsNone(grade.check_blank_answers(r))

    def test_unknown_qtype_still_rejected(self):
        with self.assertRaises(grade.ApiError):
            self.req({"card": "架空.md", "qtype": "nope", "answers": {}})

    def test_handle_request_skip_does_not_call_claude(self):
        c = F.parse(F.make_card(claim="X", why="Y", scene="- Z"))
        c["sha"] = "s"
        saved = {}
        raw = json.dumps({"card": "架空.md", "qtype": "skip", "answers": {}, "question": "Q"}).encode()
        with mock.patch.object(grade, "today_jst", return_value=TODAY), \
             mock.patch.object(grade, "reserve_daily_slot"), \
             mock.patch.object(grade, "load_card", return_value=c), \
             mock.patch.object(grade, "fetch_grade", side_effect=AssertionError("Claude を呼んだ")), \
             mock.patch.object(grade, "save_card", side_effect=lambda f, t, s, d: saved.update(t=t)):
            status, resp = grade.handle_request(str(len(raw)), io.BytesIO(raw))
        self.assertEqual((status, resp["type"]), (200, "skip"))
        self.assertIn("| スキップ | - | スキップ |", saved["t"])


def lcard(name, book, filled=True, history="", next_review="2026-11-10"):
    claim = "X" if filled else "下書き\n\n" + F.DRAFT_CLAIM
    text = F.make_card(claim=claim if filled else F.DRAFT_CLAIM, why="Y" if filled else F.WHY_MARK,
                       scene="- Z" if filled else "- " + F.SCENE_MARK, history=history,
                       status="要復習" if filled else "未履修", next_review=next_review, book=book)
    return F.parse(text, path=name + ".md")


SKIP = "\n- 2026-11-15 | スキップ | - | スキップ | 7日間出さない"


class Select(unittest.TestCase):
    def setUp(self):
        p1 = mock.patch.object(lib, "today_jst", return_value=TODAY)
        p2 = mock.patch.dict("os.environ", {"RECALL_RECENT_WINDOW": ""})
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_is_skipped_until_next_review(self):
        c = lcard("a", "本A", history=SKIP, next_review="2026-11-22")
        self.assertTrue(lib.is_skipped(c, TODAY))
        self.assertTrue(lib.is_skipped(c, date(2026, 11, 21)))
        self.assertFalse(lib.is_skipped(c, date(2026, 11, 22)))

    def test_not_skipped_when_latest_row_is_not_skip(self):
        later = "\n- 2026-11-16 | 想起 | 6/8 | 合格 | x" + SKIP
        self.assertFalse(lib.is_skipped(lcard("a", "本A", history=later, next_review="2026-11-22"), TODAY))
        self.assertFalse(lib.is_skipped(lcard("a", "本A"), TODAY))

    def test_skipped_unfilled_card_is_not_picked_next_day(self):
        skipped = lcard("a", "本A", filled=False, history=SKIP, next_review="2026-11-22")
        other = lcard("b", "本B", filled=False)
        with mock.patch.object(lib, "today_jst", return_value=date(2026, 11, 16)):
            picked = lib.select_cards([skipped, other], limit=3)
        self.assertEqual([c["path"] for c, _ in picked], ["b.md"])

    def test_skipped_card_returns_when_due(self):
        skipped = lcard("a", "本A", filled=False, history=SKIP, next_review="2026-11-22")
        with mock.patch.object(lib, "today_jst", return_value=date(2026, 11, 22)):
            picked = lib.select_cards([skipped], limit=3)
        self.assertEqual([(c["path"], q) for c, q in picked], [("a.md", "fill")])

    def test_same_book_other_card_can_appear(self):
        skipped = lcard("a", "本A", history=SKIP, next_review="2026-11-22")
        sibling = lcard("b", "本A")
        picked = lib.select_cards([skipped, sibling], limit=3)
        self.assertEqual([c["path"] for c, _ in picked], ["b.md"])

    def test_without_skip_selection_unchanged(self):
        cards = [lcard("a", "本A"), lcard("b", "本B", filled=False), lcard("c", "本C")]
        with mock.patch.object(lib, "is_skipped", return_value=False):
            base = [(c["path"], q) for c, q in lib.select_cards(cards, limit=3)]
        self.assertEqual([(c["path"], q) for c, q in lib.select_cards(cards, limit=3)], base)


class Page(unittest.TestCase):
    def test_page_has_skip_button_and_reads_skip_rows(self):
        t = select_and_post.PAGE_TEMPLATE
        self.assertIn('class="skip"', t)
        self.assertIn("'skip'", t)
        self.assertIn("スキップ済み", t)
        self.assertIn("e.label === 'スキップ'", t)
        # format が通る（{{ }} の崩れがない）
        t.format(date="2026-11-15", grade_endpoint_js='""', date_js='""', cards_api_js='""')


if __name__ == "__main__":
    unittest.main()
