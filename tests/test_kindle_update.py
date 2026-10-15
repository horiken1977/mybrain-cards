"""kindle_update.py（Cards scripts/kindle_update.py の写し。一覧 recent_books.json を書く部分）の試験。
架空のカード・架空の raw だけ。ネットワークなし。lib.py は同じフォルダの写しを読む。
回し方: python3 test_kindle_update.py   （pytest は無いので unittest をファイルから直接回す）"""
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
import kindle_update as ku  # noqa: E402

TODAY = date(2026, 10, 15)


def info(book, *srcs):
    return {"book": book, "src": list(srcs)}


def raw_file(d, name, last=None):
    fm = "---\ntitle: x\n" + (f"last_highlighted: {last}\n" if last else "") + "---\n\n本文\n"
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(fm)


class RecentBooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def run_rb(self, infos):
        return ku.recent_books(self.d, infos, TODAY)

    def test_window_inside_outside_and_edges(self):
        raw_file(self.d, "a.md", "2026-10-15")  # 今日
        raw_file(self.d, "b.md", "2025-10-15")  # 365日前（境目・内）
        raw_file(self.d, "c.md", "2025-10-14")  # 366日前（外）
        raw_file(self.d, "d.md", "2026-04-15")  # 183日前（境目・内）
        raw_file(self.d, "e.md", "2026-04-14")  # 184日前（外）
        r = self.run_rb([info(n.upper(), n + ".md") for n in "abcde"])
        self.assertEqual(r["365"], ["A", "B", "D", "E"])
        self.assertEqual(r["183"], ["A", "D"])

    def test_no_date_and_future_are_excluded(self):
        raw_file(self.d, "a.md")
        raw_file(self.d, "b.md", "2026-10-16")
        r = self.run_rb([info("A", "a.md"), info("B", "b.md")])
        self.assertEqual(r, {"365": [], "183": []})

    def test_book_without_card_is_not_listed(self):
        raw_file(self.d, "a.md", "2026-10-01")
        raw_file(self.d, "nocard.md", "2026-10-01")
        r = self.run_rb([info("A", "a.md")])
        self.assertEqual(r["365"], ["A"])

    def test_missing_raw_file_and_empty_book(self):
        raw_file(self.d, "a.md", "2026-10-01")
        r = self.run_rb([info("X", "gone.md"), info("", "a.md")])
        self.assertEqual(r, {"365": [], "183": []})

    def test_same_book_name_is_listed_once(self):
        raw_file(self.d, "a1.md", "2026-10-01")
        raw_file(self.d, "a2.md", "2025-01-01")
        r = self.run_rb([info("A", "a1.md"), info("A", "a2.md"), info("A", "a1.md", "a2.md")])
        self.assertEqual(r["365"], ["A"])

    def test_second_edition_in_window_counts(self):
        raw_file(self.d, "old.md", "2024-01-01")
        raw_file(self.d, "new.md", "2026-09-01")
        r = self.run_rb([info("A", "old.md", "new.md")])
        self.assertEqual(r["183"], ["A"])

    def test_no_dates_in_output(self):
        raw_file(self.d, "a.md", "2026-10-01")
        s = json.dumps(self.run_rb([info("A", "a.md")]))
        self.assertNotIn("2026-10-01", s)


class MainWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cards = os.path.join(self.tmp.name, "cards")
        self.kindle = os.path.join(self.tmp.name, "kindle")
        os.makedirs(self.cards)
        os.makedirs(os.path.join(self.kindle, ".sync"))
        with open(os.path.join(self.kindle, ".sync", "sync.py"), "w") as f:
            f.write("def parse_file(p):\n    return {}, []\n")
        raw_file(self.kindle, "a.md", "2026-10-01")
        raw_file(self.kindle, "b.md", "2024-01-01")
        for name, book, src in (("カードA", "本A", "a.md"), ("カードB", "本B", "b.md")):
            with open(os.path.join(self.cards, name + ".md"), "w", encoding="utf-8") as f:
                f.write(f'---\ntitle: {name}\ntype: card\nbook: "[[entities/{book}]]"\nstatus: 学習中\n'
                        f"priority: 中\nlast_reviewed: 2026-10-01\nnext_review: 2026-10-20\nstreak: 1\ntags: [t]\n---\n"
                        f"\n## 主張\n\nX\n\n## 根拠\n\n> q\n\n原本：`raw/kindle/{src}`\n\n## 回答履歴\n")
        self.path = os.path.join(self.cards, "recent_books.json")
        self.before = {n: open(os.path.join(self.cards, n), "rb").read() for n in os.listdir(self.cards)}
        sys.modules.pop("sync", None)

    def run_main(self, *extra):
        argv = ["kindle_update.py", "--kindle", self.kindle, "--no-ai", *extra]
        with mock.patch.object(ku, "CARDS_DIR", self.cards), \
             mock.patch.object(ku, "RECENT_BOOKS_PATH", self.path), \
             mock.patch.object(lib, "today_jst", lambda: TODAY), \
             mock.patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(io.StringIO()):
            ku.main()

    def test_writes_even_without_changes(self):
        self.run_main()  # changes.json なし = 新しいハイライトがない晩
        data = json.load(open(self.path, encoding="utf-8"))
        self.assertEqual(data["updated"], "2026-10-15")
        self.assertEqual(data["windows"], {"365": ["本A"], "183": ["本A"]})
        for n, b in self.before.items():  # カードは変わらない
            self.assertEqual(open(os.path.join(self.cards, n), "rb").read(), b)

    def test_dry_run_writes_nothing(self):
        self.run_main("--dry-run")
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
