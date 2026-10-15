"""lib.py（Cards scripts/lib.py の写し。案 B）の試験。架空のカードだけ・ネットワークなし。
回し方: python3 test_lib.py   （pytest は無いので unittest をファイルから直接回す）"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lib  # noqa: E402

TODAY = date(2026, 10, 15)
CLAIM_MARK = "（AIの下書き。初回の /recall で自分の言葉に直す）"
FAIL = "\n- 2026-10-14 | 想起 | 0/8 | 不合格 | 架空"


def card(name, book, filled=True, priority="中", history="", next_review="2026-10-10", memo=False):
    claim = "X" if filled else "下書き\n\n" + CLAIM_MARK
    ev = "引用" + ("\n\n- 自分のメモ（location 1）：架空" if memo else "")
    text = (f'---\ntitle: {name}\ntype: card\nbook: "[[entities/{book}]]"\nstatus: 要復習\n'
            f"priority: {priority}\nlast_reviewed: 2026-10-01\nnext_review: {next_review}\nstreak: 0\ntags: [t]\n---\n"
            f"\n## 主張\n\n{claim}\n\n## 根拠\n\n{ev}\n\n## なぜ自分に重要か\n\nY\n\n## 使う場面\n\n- Z\n\n## 回答履歴\n{history}")
    head, body = text[4:].split("\n---\n", 1)
    fm = {}
    for line in head.split("\n"):
        k, _, v = line.partition(":")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        fm[k.strip()] = v
    return {"path": name + ".md", "fm": fm, "body": body, "raw": text}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "recent_books.json")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("RECALL_RECENT_WINDOW", None)
        for p in (mock.patch.object(lib, "today_jst", lambda: TODAY),
                  mock.patch.object(lib, "MAX_FILL_PER_DAY", 1),
                  mock.patch.object(lib, "RECENT_BOOKS_PATH", self.path)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, obj=None, raw=None):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(obj, ensure_ascii=False))

    def good(self, updated="2026-10-15", w365=("新1", "新2"), w183=("新1",)):
        self.write({"updated": updated, "windows": {"365": list(w365), "183": list(w183)}})

    def pick(self, cards, limit=3):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            r = lib.select_cards(cards, limit)
        self.last_log = err.getvalue()
        return [(c["path"], q) for c, q in r]


def cards_due():
    # 採点の問いの候補：古い本（優先度 高・不合格）が今の並びでは先頭
    return [card("古A", "旧A", priority="高", history=FAIL), card("古B", "旧B", priority="高"),
            card("新1", "新1"), card("新2", "新2", priority="低", next_review="2026-10-14")]


class NoVariable(Base):
    def test_unset_keeps_current_order(self):
        self.good()
        base = self.pick(cards_due(), 3)
        self.assertEqual([p for p, _ in base], ["古A.md", "古B.md", "新1.md"])
        self.assertEqual(self.last_log, "")  # 変数なしはログも出さない

    def test_unlisted_value_is_ignored_with_log(self):
        self.good()
        for v in ("90", "abc", "1"):
            os.environ["RECALL_RECENT_WINDOW"] = v
            self.assertEqual([p for p, _ in self.pick(cards_due(), 3)], ["古A.md", "古B.md", "新1.md"])
            self.assertIn("今の並びのまま", self.last_log)


class Windows(Base):
    def test_365_puts_recent_books_first(self):
        self.good()
        os.environ["RECALL_RECENT_WINDOW"] = "365"
        r = self.pick(cards_due(), 3)
        self.assertEqual([p for p, _ in r][:2], ["新1.md", "新2.md"])  # 優先度 低 でも直近の本が先
        self.assertEqual(len(r), 3)
        self.assertIn("2冊", self.last_log)

    def test_183_uses_only_the_half_year_books(self):
        self.good()
        os.environ["RECALL_RECENT_WINDOW"] = "183"
        r = self.pick(cards_due(), 3)
        self.assertEqual(r[0][0], "新1.md")
        self.assertEqual(r[1][0], "古A.md")  # 新2 は半年の窓の外なので今の並び（不合格が先）

    def test_fill_candidates_recent_first_within_fill_cap(self):
        cs = [card("旧補完", "旧A", filled=False, priority="高"), card("新補完", "新1", filled=False, priority="低")]
        self.good()
        self.assertEqual(self.pick(cs, 1), [("旧補完.md", "fill")])
        os.environ["RECALL_RECENT_WINDOW"] = "365"
        self.assertEqual(self.pick(cs, 1), [("新補完.md", "fill")])

    def test_inside_recent_current_rules_still_apply(self):
        self.good(w365=("新1", "新2"))
        os.environ["RECALL_RECENT_WINDOW"] = "365"
        cs = [card("新1低", "新1", priority="低"), card("新2不合格", "新2", history=FAIL)]
        self.assertEqual([p for p, _ in self.pick(cs, 2)], ["新2不合格.md", "新1低.md"])

    def test_recent_exhausted_falls_back_to_current_order(self):
        self.good(w365=("新1",))
        os.environ["RECALL_RECENT_WINDOW"] = "365"
        # 直近の本は1冊（同じ本は1日1枚）。残りの枠は今の並びの順
        r = self.pick(cards_due(), 3)
        self.assertEqual([p for p, _ in r], ["新1.md", "古A.md", "古B.md"])

    def test_recent_fill_exhausted_goes_to_graded(self):
        cs = [card("新補完", "新1", filled=False), card("旧補完", "旧A", filled=False, priority="高"),
              card("旧採点", "旧B")]
        self.good(w365=("新1",))
        os.environ["RECALL_RECENT_WINDOW"] = "365"
        r = self.pick(cs, 3)
        self.assertEqual(r[0], ("新補完.md", "fill"))
        self.assertEqual(r[1][0], "旧採点.md")  # 補完は1日1問まで。残りは今の並び（採点が先）
        self.assertEqual(r[2], ("旧補完.md", "fill"))  # 足りない枠は補完で埋める


class UnusableList(Base):
    def check_fallback(self, needle):
        os.environ["RECALL_RECENT_WINDOW"] = "365"
        r = self.pick(cards_due(), 3)
        self.assertEqual([p for p, _ in r], ["古A.md", "古B.md", "新1.md"])
        self.assertIn("今の並びのまま", self.last_log)
        self.assertIn(needle, self.last_log)

    def test_missing_file(self):
        self.check_fallback("FileNotFoundError")

    def test_broken_json(self):
        self.write(raw="{not json")
        self.check_fallback("JSONDecodeError")

    def test_missing_keys_and_wrong_types(self):
        for obj in ({"windows": {"365": []}}, {"updated": "2026-10-15"}, {"updated": "2026-10-15", "windows": {}},
                    {"updated": "2026-10-15", "windows": {"365": "新1"}}, {"updated": "2026-10-15", "windows": {"365": [1]}},
                    {"updated": "昨日", "windows": {"365": ["新1"]}}, [1, 2]):
            self.write(obj)
            self.check_fallback("")

    def test_stale_list(self):
        self.good(updated="2026-10-11")  # 4日前
        self.check_fallback("古い")

    def test_three_days_old_is_still_used(self):
        self.good(updated="2026-10-12")  # ちょうど3日前
        os.environ["RECALL_RECENT_WINDOW"] = "365"
        self.assertEqual(self.pick(cards_due(), 3)[0][0], "新1.md")


class WriteFrontmatterUnchanged(Base):
    def test_only_nine_keys_written(self):
        out = lib.write_frontmatter(card("a", "新1"), {"status": "学習中", "extra": "x"})
        head = out.split("\n---\n", 1)[0]
        self.assertEqual([l.split(":")[0] for l in head.split("\n")[1:]],
                         ["title", "type", "book", "status", "priority", "last_reviewed", "next_review", "streak", "tags"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
