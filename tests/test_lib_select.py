"""Question order (scripts/lib.select_cards): a card whose last graded answer failed comes back
first the next day (2026-10-04〜, ToDo B-4). No network; only invented cards."""
import unittest
from datetime import date
from unittest import mock

import card_fixtures as F
import lib

FAIL_HIST = ("\n- 2026-10-03 | 想起 | 0/8 | 不合格 | 架空のフィードバック"
             "\n  - 問い: 架空の問い"
             "\n  - 回答: 架空の回答"
             "\n- 2026-10-01 | 想起 | 6/8 | 合格 | 架空のフィードバック")
PASS_HIST = "\n- 2026-10-03 | 想起 | 6/8 | 合格 | 架空のフィードバック"


def card(path, book, priority="中", history="", next_review="2026-10-04"):
    text = F.make_card(claim="X", why="Y", scene="- Z", history=history, status="要復習",
                       next_review=next_review, book=book).replace("priority: 高", f"priority: {priority}")
    return F.parse(text, path)


class SelectFailedFirstTest(unittest.TestCase):
    def pick(self, cards, today=date(2026, 10, 4)):
        with mock.patch.object(lib, "today_jst", lambda: today), \
                mock.patch.object(lib, "MAX_FILL_PER_DAY", 1):
            return [c["path"] for c, _ in lib.select_cards(cards, limit=2)]

    def test_last_failed_reads_the_newest_graded_line(self):
        self.assertTrue(lib.last_failed(card("a.md", "本A", history=FAIL_HIST)))
        self.assertFalse(lib.last_failed(card("b.md", "本B", history=PASS_HIST)))
        self.assertFalse(lib.last_failed(card("c.md", "本C")))
        # 補完とテストの回答は飛ばして、その下の採点ありの行で決める
        fill_then_fail = "\n- 2026-10-04 | 補完 | - | - | 初回記入(web)" + FAIL_HIST
        self.assertTrue(lib.last_failed(card("d.md", "本D", history=fill_then_fail)))
        test_pass_then_fail = "\n- 2026-10-04 | 想起 | 7/8 | 合格(テスト) | 架空" + FAIL_HIST
        self.assertTrue(lib.last_failed(card("e.md", "本E", history=test_pass_then_fail)))

    def test_failed_cards_come_before_priority_and_overdue(self):
        """優先度「高」や長く遅れたカードがあっても、前回不合格の2枚が先に選ばれる。"""
        high = card("high.md", "本H", priority="高", history=PASS_HIST)
        old = card("old.md", "本O", history=PASS_HIST, next_review="2026-09-28")
        f1 = card("f1.md", "本F1", history=FAIL_HIST)
        f2 = card("f2.md", "本F2", history=FAIL_HIST)
        self.assertEqual(self.pick([high, old, f1, f2]), ["f1.md", "f2.md"])

    def test_failed_card_wins_the_book_slot(self):
        """同じ本の、長く遅れたカードより先に、前回不合格のカードがその本の枠を取る。"""
        same_book_old = card("old.md", "本S", history=PASS_HIST, next_review="2026-10-01")
        failed = card("failed.md", "本S", history=FAIL_HIST)
        self.assertEqual(self.pick([same_book_old, failed]), ["failed.md"])

    def test_without_failures_the_order_is_unchanged(self):
        """不合格がなければ、これまでどおり priority → 超過日数の順。"""
        high = card("high.md", "本H", priority="高", history=PASS_HIST)
        old = card("old.md", "本O", history=PASS_HIST, next_review="2026-09-28")
        new = card("new.md", "本N", history=PASS_HIST)
        self.assertEqual(self.pick([new, old, high]), ["high.md", "old.md"])


LOW_HIST = ("\n- 2026-10-03 | 想起 | 6/8 | 合格 | 架空"
            "\n- 2026-10-02 | 想起 | 0/8 | 不合格 | 架空"
            "\n- 2026-10-01 | 想起 | 0/8 | 不合格 | 架空")


class SelectPassRateTest(unittest.TestCase):
    pick = SelectFailedFirstTest.pick

    def test_parse_history_counts_graded_lines_only(self):
        h = ("\n- 2026-10-05 | 補完 | - | - | 初回記入(web)"
             "\n- 2026-10-04 | 想起 | 7/8 | 合格(テスト) | 架空" + LOW_HIST)
        self.assertEqual(lib.parse_history(card("a.md", "本A", history=h)), (3, 1))
        self.assertEqual(lib.parse_history(card("b.md", "本B")), (0, 0))

    def test_lower_pass_rate_comes_first(self):
        high = card("high.md", "本H", priority="高", history=PASS_HIST)
        low = card("low.md", "本L", history=LOW_HIST)
        mid_hist = PASS_HIST + "\n- 2026-10-01 | 想起 | 0/8 | 不合格 | 架空"
        mid = card("mid.md", "本M", history=mid_hist)
        self.assertEqual(self.pick([high, mid, low]), ["low.md", "mid.md"])

    def test_no_history_is_not_pushed_forward(self):
        high = card("high.md", "本H", priority="高")
        old = card("old.md", "本O", next_review="2026-09-28")
        passed = card("p.md", "本P", history=PASS_HIST)
        self.assertEqual(self.pick([passed, old, high]), ["high.md", "old.md"])

    def test_last_failed_still_beats_pass_rate(self):
        low = card("low.md", "本L", history=LOW_HIST)  # 最新は合格・正答率 1/3
        failed = card("failed.md", "本F", history=FAIL_HIST)  # 最新が不合格・正答率 1/2
        self.assertEqual(self.pick([low, failed]), ["failed.md", "low.md"])


class StableIntervalTest(unittest.TestCase):
    """安定の次回確認日：初めて上がったときは30日後、安定のまま合格したら60日後（2026-10-09〜、ToDo B-4）"""

    def test_first_stable_is_30_days_and_staying_stable_is_60(self):
        with mock.patch.object(lib, "today_jst", lambda: date(2026, 10, 9)):
            self.assertEqual(lib.next_review_after_pass("安定", "要確認"), "2026-11-08")
            self.assertEqual(lib.next_review_after_pass("安定", "安定"), "2026-12-08")
            self.assertEqual(lib.next_review_after_pass("要確認", "学習中"), "2026-10-16")


class ContrastPartnerTest(unittest.TestCase):
    """対比の相手：記入済みで別の本のカードから、カードと日付で決まる1枚（2026-10-09〜、ToDo B-4）"""

    def setUp(self):
        self.me = card("me.md", "本M")
        self.same_book = card("same.md", "本M")
        self.unfilled = F.parse(F.make_card(book="本U"), "unfilled.md")
        self.others = [card(f"o{i}.md", f"本{i}") for i in range(5)]
        self.all = [self.me, self.same_book, self.unfilled] + self.others

    def partner(self, day, me=None):
        return lib.other_card_for_contrast(me or self.me, self.all, today=day)["path"]

    def test_partner_is_filled_and_from_another_book(self):
        allowed = {c["path"] for c in self.others}
        for d in range(1, 31):
            self.assertIn(self.partner(date(2026, 10, d)), allowed)

    def test_same_day_same_partner_and_it_changes_over_days(self):
        day = date(2026, 10, 9)
        self.assertEqual(self.partner(day), self.partner(day))
        partners = {self.partner(date(2026, 10, d)) for d in range(1, 31)}
        self.assertGreaterEqual(len(partners), 3)

    def test_different_cards_do_not_all_get_the_same_partner(self):
        day = date(2026, 10, 9)
        partners = {lib.other_card_for_contrast(c, self.all, today=day)["path"] for c in self.others}
        self.assertGreater(len(partners), 1)

    def test_none_when_no_filled_card_of_another_book(self):
        self.assertIsNone(lib.other_card_for_contrast(self.me, [self.me, self.same_book, self.unfilled],
                                                      today=date(2026, 10, 9)))


if __name__ == "__main__":
    unittest.main()
