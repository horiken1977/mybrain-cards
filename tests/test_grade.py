"""Tests for api/grade.py (設計書 v4 §5). Run: cd mybrain/cards && python3 -m unittest discover -s tests -v

No network: urlopen is replaced by a function that fails the test, GitHub by FakeGitHub and
Claude by a counting fake. Only invented cards are used."""
import base64
import io
import json
import re
import unittest
import urllib.error
import urllib.parse
from datetime import date
from unittest import mock

import card_fixtures as F
import grade
import lib

TODAY = date(2026, 10, 2)
CARD = "架空のカード.md"
PASS = {"accuracy": 2, "example": 2, "conditions": 1, "action": 1,
        "feedback": "架空のフィードバック", "model_answer": "架空の模範解答"}
FAIL = {"accuracy": 0, "example": 2, "conditions": 2, "action": 2,
        "feedback": "架空の不合格の一言", "model_answer": "架空の模範解答"}
FILL_PQR = {"claim": "P", "why": "Q", "scene": "R"}
DEFAULT_RETRY_WAIT_SEC = grade.RETRY_WAIT_SEC  # 差し替える前の値
GRADED_KEYS = {"ok", "type", "score", "passed", "feedback", "model_answer", "new_status", "next_review", "advanced"}


def http_error(code, msg="error"):
    return urllib.error.HTTPError("https://api.github.com/x", code, msg, {}, None)


class FakeGitHub:
    """Stands in for grade.gh_request. Holds {name: (text, sha)}; PUT with a stale sha is 409."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.calls = []          # (method, name)
        self.put_shas = []
        self.messages = []
        self.before_put = None   # callable(fake, n) run before the n-th PUT (1-based)
        self.put_errors = {}     # {n: exception} raised at the n-th PUT
        self.get_errors = {}     # {n: exception} raised at the n-th GET
        self._seq = 0

    def new_sha(self):
        self._seq += 1
        return f"sha{self._seq}"

    def add(self, name, text):
        self.files[name] = (text, self.new_sha())

    def text(self, name):
        return self.files[name][0]

    def sha(self, name):
        return self.files[name][1]

    @property
    def gets(self):
        return sum(1 for m, _ in self.calls if m == "GET")

    @property
    def puts(self):
        return sum(1 for m, _ in self.calls if m == "PUT")

    def __call__(self, method, path, body=None):
        p = path.split("?", 1)[0]
        assert p.startswith("contents/"), path
        quoted = p[len("contents/"):]
        assert "/" not in quoted, path
        name = urllib.parse.unquote(quoted)
        self.calls.append((method, name))
        if method == "GET":
            n = self.gets
            if n in self.get_errors:
                raise self.get_errors[n]
            if name not in self.files:
                raise http_error(404, "Not Found")
            text, sha = self.files[name]
            return {"content": base64.b64encode(text.encode("utf-8")).decode("ascii"), "sha": sha}
        assert method == "PUT"
        n = self.puts
        if self.before_put:
            self.before_put(self, n)
        if n in self.put_errors:
            raise self.put_errors[n]
        self.put_shas.append(body["sha"])
        if name not in self.files or body["sha"] != self.files[name][1]:
            raise http_error(409, "Conflict")
        self.messages.append(body["message"])
        self.files[name] = (base64.b64decode(body["content"]).decode("utf-8"), self.new_sha())
        return {}


class GradeTestBase(unittest.TestCase):
    def setUp(self):
        def no_network(*a, **k):
            raise AssertionError("urlopen must not be called in tests")

        self.fake = FakeGitHub()
        self.claude_calls = []
        self.grade_result = dict(PASS)
        self.sleeps = []
        self.log = io.StringIO()
        self.today_calls = 0
        self.today_seq = [TODAY]

        def fake_claude(title, claim, evidence, qtype_label, answer):
            self.claude_calls.append((title, claim, evidence, qtype_label, answer))
            return dict(self.grade_result)

        def fake_today():
            self.today_calls += 1
            return self.today_seq[min(self.today_calls - 1, len(self.today_seq) - 1)]

        for target, attr, value in (
            (grade.urllib.request, "urlopen", no_network),
            (grade, "gh_request", self.fake),
            (grade, "grade_with_claude", fake_claude),
            (grade, "today_jst", fake_today),
            (grade, "RETRY_WAIT_SEC", 0),
            (grade.time, "sleep", self.sleeps.append),
            (grade.sys, "stderr", self.log),
        ):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- helpers
    def post(self, payload, ascii_json=False):
        data = json.dumps(payload, ensure_ascii=ascii_json).encode("utf-8")
        return grade.handle_request(str(len(data)), io.BytesIO(data))

    def post_raw(self, data, content_length="auto"):
        cl = str(len(data)) if content_length == "auto" else content_length
        return grade.handle_request(cl, io.BytesIO(data))

    def graded(self, text=None, qtype="apply", answer="架空の回答", question="架空の問い", card=CARD, **extra):
        payload = {"date": "2026-10-02", "card": card, "qtype": qtype, "question": question,
                   "answers": {"text": answer}}
        payload.update(extra)
        return self.post(payload)

    def fill(self, answers=FILL_PQR, card=CARD, **extra):
        payload = {"date": "2026-10-02", "card": card, "qtype": "fill", "question": "",
                   "answers": dict(answers)}
        payload.update(extra)
        return self.post(payload)

    def snapshot(self, name=CARD):
        return self.fake.files[name]

    def assertNoExternal(self):
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.claude_calls, [])

    def assertError(self, result, status, code):
        self.assertEqual(result, (status, {"ok": False, "error": code}))


# ====================================================================== A
class TestNormalFlow(GradeTestBase):
    def test_A1_first_pass_of_the_day(self):
        self.fake.add(CARD, F.A)
        status, resp = self.graded()
        self.assertEqual(status, 200)
        self.assertEqual(resp["new_status"], "要確認")
        self.assertEqual(resp["next_review"], "2026-10-09")
        self.assertTrue(resp["advanced"])
        self.assertTrue(resp["passed"])
        self.assertEqual(resp["score"]["total"], 6)
        text = self.fake.text(CARD)
        self.assertIn("status: 要確認\n", text)
        self.assertIn("streak: 2\n", text)
        self.assertIn("next_review: 2026-10-09\n", text)
        self.assertIn("last_reviewed: 2026-10-02\n", text)
        self.assertIn("## 回答履歴\n"
                      "\n- 2026-10-02 | 適用 | 6/8 | 合格 | 架空のフィードバック"
                      "\n  - 問い: 架空の問い"
                      "\n  - 回答: 架空の回答"
                      "\n  - 内訳: 正確さ2・具体例2・適用条件1・次の行動1"
                      "\n  - 模範解答例: 架空の模範解答"
                      "\n- 2026-09-29 | 想起 | 6/8 | 合格 | 前回の一言", text)
        self.assertEqual((self.fake.gets, self.fake.puts, len(self.claude_calls)), (1, 1, 1))
        self.assertEqual(self.fake.put_shas, ["sha1"])
        evidence = "> 架空の引用文。\n\n[Kindle location 10](https://example.com/10)"  # get_evidence は「原本」の前まで
        self.assertEqual(self.claude_calls[0], ("架空のカード", "架空の主張。", evidence, "適用", "架空の回答"))

    def test_A2_fail(self):
        self.fake.add(CARD, F.A)
        self.grade_result = dict(FAIL)
        status, resp = self.graded()
        self.assertEqual(status, 200)
        self.assertFalse(resp["passed"])
        self.assertEqual((resp["new_status"], resp["next_review"], resp["advanced"]), ("要復習", "2026-10-03", False))
        text = self.fake.text(CARD)
        self.assertIn("status: 要復習\n", text)
        self.assertIn("streak: 0\n", text)
        self.assertIn("| 6/8 | 不合格 |", text)

    def test_A3_second_pass_same_day(self):
        card = F.make_card(claim="架空の主張。", why="理由", scene="- 場面", status="要確認", streak="2",
                           last_reviewed="2026-10-02", next_review="2026-10-09",
                           history="\n- 2026-10-02 | 想起 | 6/8 | 合格 | 今日の一言")
        self.fake.add(CARD, card)
        status, resp = self.graded()
        self.assertEqual(status, 200)
        self.assertEqual((resp["new_status"], resp["next_review"], resp["advanced"]), ("要確認", "2026-10-09", False))
        text = self.fake.text(CARD)
        for line in ("status: 要確認", "next_review: 2026-10-09", "streak: 2"):
            self.assertIn("\n" + line + "\n", text)
        self.assertEqual(len(re.findall(r"^- 2026-10-02 \|", text, re.M)), 2)

    def test_A4_fill_matches_previous_output(self):
        exp_hist = "\n- 2026-10-02 | 補完 | - | - | 初回記入(web)"
        for src, scene in ((F.B, "- R"), (F.B2, "- 部下・チーム：R")):
            with self.subTest(scene=scene):
                self.fake.add(CARD, src)
                self.fake.calls.clear()
                status, resp = self.fill()
                self.assertEqual((status, resp), (200, {"ok": True, "type": "fill"}))
                expected = F.make_card(claim="P", why="Q", scene=scene, history=exp_hist, status="要復習",
                                       last_reviewed="2026-10-02", next_review="2026-10-03")
                self.assertEqual(self.fake.text(CARD), expected)
                self.assertEqual(self.claude_calls, [])

    def test_A4b_already_filled_on_first_read(self):
        for key in ("c", "c*", "c†", "c‡", "d†"):
            with self.subTest(card=key):
                self.fake.add(CARD, F.ALL[key])
                before = self.snapshot()
                self.fake.calls.clear()
                result = self.fill()
                self.assertError(result, 409, "already_filled")
                self.assertEqual((self.fake.gets, self.fake.puts), (1, 0))
                self.assertEqual(self.claude_calls, [])
                self.assertEqual(self.snapshot(), before)

    def test_A5_response_shape(self):
        self.fake.add(CARD, F.A)
        _, resp = self.graded()
        self.assertEqual(set(resp), GRADED_KEYS)
        self.assertEqual(set(resp["score"]), {"accuracy", "example", "conditions", "action", "total"})
        self.fake.add(CARD, F.B)
        _, resp = self.fill()
        self.assertEqual(set(resp), {"ok", "type"})

    def test_A6_page_payload_shape(self):
        for qtype in ("recall", "apply", "contrast", "refute", "fill"):
            with self.subTest(qtype=qtype):
                answers = dict(FILL_PQR) if qtype == "fill" else {"text": " 回答 "}
                payload = {"date": "2026-10-02", "card": CARD, "qtype": qtype, "question": "問い", "answers": answers}
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = grade.read_and_validate(str(len(data)), io.BytesIO(data), TODAY)
                self.assertIsInstance(req, grade.ValidRequest)
                self.assertEqual(req._fields, ("card", "qtype", "answer_text", "fill_answers", "question", "date"))
                self.assertEqual((req.card, req.qtype, req.question, req.date), (CARD, qtype, "問い", "2026-10-02"))
                if qtype == "fill":
                    self.assertEqual(req.fill_answers, FILL_PQR)
                    self.assertEqual(req.answer_text, "")
                else:
                    self.assertIsNone(req.fill_answers)
                    self.assertEqual(req.answer_text, "回答")

    def test_A7_interval_to_stable(self):
        for streak, exp_streak, exp_next in (("2", "3", "2026-12-01"), ("0", "1", "2026-11-01")):
            with self.subTest(streak=streak):
                self.fake.add(CARD, F.make_card(claim="主張。", why="理由", scene="- 場面", status="要確認", streak=streak))
                _, resp = self.graded()
                self.assertEqual((resp["new_status"], resp["next_review"]), ("安定", exp_next))
                self.assertIn(f"streak: {exp_streak}\n", self.fake.text(CARD))

    def test_A8_pass_while_stable(self):
        self.fake.add(CARD, F.make_card(claim="主張。", why="理由", scene="- 場面", status="安定", streak="3"))
        _, resp = self.graded()
        self.assertEqual((resp["new_status"], resp["next_review"]), ("安定", "2026-12-01"))
        self.assertIn("streak: 4\n", self.fake.text(CARD))


# ====================================================================== B
class TestRetry(GradeTestBase):
    def test_B1_graded_conflict_then_success(self):
        self.fake.add(CARD, F.A)

        def interrupt(fake, n):
            if n == 1:
                fake.add(CARD, fake.text(CARD) + "\n割り込みの行")
        self.fake.before_put = interrupt
        status, resp = self.graded()
        self.assertEqual(status, 200)
        self.assertEqual(set(resp), GRADED_KEYS)
        self.assertEqual((len(self.claude_calls), self.fake.gets, self.fake.puts), (1, 2, 2))
        self.assertEqual(self.fake.put_shas, ["sha1", "sha2"])
        text = self.fake.text(CARD)
        self.assertIn("割り込みの行", text)
        self.assertIn("- 2026-10-02 | 適用 | 6/8 | 合格 |", text)
        self.assertEqual(self.sleeps, [0])
        self.assertEqual(self.log.getvalue(), f"save conflict on {CARD}; retrying once\n")  # 回答本文は出さない

    def test_B2_one_step_per_day_after_retry(self):
        self.fake.add(CARD, F.A)
        passed_today = F.make_card(claim="架空の主張。", why="架空の理由。", scene="- 架空の場面", status="要確認",
                                   streak="2", last_reviewed="2026-10-02", next_review="2026-10-09",
                                   history="\n- 2026-10-02 | 想起 | 6/8 | 合格 | 別タブの合格"
                                           "\n- 2026-09-29 | 想起 | 6/8 | 合格 | 前回の一言")
        self.fake.before_put = lambda fake, n: n == 1 and fake.add(CARD, passed_today)
        status, resp = self.graded()
        self.assertEqual(status, 200)
        self.assertEqual((resp["new_status"], resp["advanced"], resp["next_review"]), ("要確認", False, "2026-10-09"))
        text = self.fake.text(CARD)
        self.assertIn("status: 要確認\n", text)
        self.assertIn("streak: 2\n", text)
        self.assertIn("next_review: 2026-10-09\n", text)
        self.assertEqual(len(self.claude_calls), 1)

    def test_B3_recomputed_from_fresh_card(self):
        for result, fresh_status, fresh_streak, exp_status, exp_streak, exp_next in (
            (PASS, "要復習", "0", "学習中", "1", "2026-10-05"),
            (FAIL, "要確認", "2", "学習中", "1", "2026-10-03"),
        ):
            with self.subTest(passed=result is PASS):
                self.grade_result = dict(result)
                self.fake.add(CARD, F.A)
                fresh = F.make_card(claim="架空の主張。", why="架空の理由。", scene="- 架空の場面",
                                    status=fresh_status, streak=fresh_streak)
                self.fake.before_put = lambda fake, n, fresh=fresh: n == 1 and fake.add(CARD, fresh)
                self.fake.calls.clear()
                status, resp = self.graded()
                self.assertEqual(status, 200)
                self.assertEqual((resp["new_status"], resp["next_review"]), (exp_status, exp_next))
                text = self.fake.text(CARD)
                self.assertIn(f"status: {exp_status}\n", text)
                self.assertIn(f"streak: {exp_streak}\n", text)

    def test_B4_graded_conflict_twice(self):
        self.fake.add(CARD, F.A)
        self.fake.before_put = lambda fake, n: fake.add(CARD, fake.text(CARD) + f"\n割り込み{n}")
        status, resp = self.graded()
        self.assertEqual(status, 409)
        self.assertEqual(set(resp), {"ok", "error", "unsaved"})
        self.assertEqual((resp["ok"], resp["error"]), (False, "save_conflict"))
        self.assertEqual(set(resp["unsaved"]), {"type", "score", "passed", "feedback", "model_answer"})
        self.assertEqual(resp["unsaved"]["type"], "graded")
        self.assertEqual(resp["unsaved"]["score"]["total"], 6)
        self.assertEqual((len(self.claude_calls), self.fake.gets, self.fake.puts), (1, 2, 2))
        self.assertNotIn("2026-10-02 | 適用", self.fake.text(CARD))

    def test_B5_fill_conflict_then_success(self):
        self.fake.add(CARD, F.B)
        memo_card = F.B.replace("原本：", "- 自分のメモ（location 2）：架空のメモ\n\n原本：")
        self.fake.before_put = lambda fake, n: n == 1 and fake.add(CARD, memo_card)
        result = self.fill()
        self.assertEqual(result, (200, {"ok": True, "type": "fill"}))
        self.assertEqual((len(self.claude_calls), self.fake.gets, self.fake.puts), (0, 2, 2))
        text = self.fake.text(CARD)
        self.assertIn("架空のメモ", text)
        self.assertIn("## なぜ自分に重要か\n\nQ\n\n## 使う場面\n\n- R\n", text)

    def test_B5b_another_fill_saved_first(self):
        for start, done in ((F.B, F.C), (F.B, F.C_STAR), (F.B, F.C_DAGGER), (F.B2, F.C_DDAGGER)):
            with self.subTest(done=F.body_of(done)[:40]):
                self.fake.add(CARD, start)
                self.fake.calls.clear()
                self.fake.before_put = lambda fake, n, done=done: n == 1 and fake.add(CARD, done)
                result = self.fill()
                self.assertError(result, 409, "already_filled")
                self.assertEqual((self.fake.gets, self.fake.puts, len(self.claude_calls)), (2, 1, 0))
                self.assertEqual(self.fake.text(CARD), done)
                self.assertEqual(self.fake.text(CARD).count("| 補完 |"), 1)

    def test_B5c_already_graded(self):
        for done in (F.D, F.D_STAR, F.D_DAGGER):
            with self.subTest(done=F.body_of(done)[:40]):
                self.fake.add(CARD, F.B)
                self.fake.calls.clear()
                self.fake.before_put = lambda fake, n, done=done: n == 1 and fake.add(CARD, done)
                sha_holder = {}
                orig = self.fake.before_put

                def remember(fake, n, orig=orig):
                    orig(fake, n)
                    sha_holder["sha"] = fake.sha(CARD)
                self.fake.before_put = remember
                result = self.fill()
                self.assertError(result, 409, "already_filled")
                self.assertEqual((self.fake.puts, len(self.claude_calls)), (1, 0))
                self.assertEqual(self.snapshot(), (done, sha_holder["sha"]))
                text = self.fake.text(CARD)
                self.assertIn("status: 学習中\n", text)
                self.assertIn("next_review: 2026-10-05\n", text)

    def test_B5d_fill_twice(self):
        for first in (dict(FILL_PQR), {"claim": F.X_STAR, "why": F.Y_STAR, "scene": F.Z_STAR},
                      {"claim": "X", "why": "Y", "scene": F.SCENE_DAGGER}):
            with self.subTest(first=first["scene"][:20]):
                self.fake.add(CARD, F.B)
                self.assertEqual(self.fill(first), (200, {"ok": True, "type": "fill"}))
                after_first = self.snapshot()
                self.fake.calls.clear()
                result = self.fill({"claim": "P2", "why": "Q2", "scene": "R2"})
                self.assertError(result, 409, "already_filled")
                self.assertEqual(self.fake.puts, 0)
                self.assertEqual(self.snapshot(), after_first)

    def test_B5e_fresh_card_partly_filled(self):
        self.fake.add(CARD, F.B)
        self.fake.before_put = lambda fake, n: n == 1 and fake.add(CARD, F.E)
        result = self.fill()
        self.assertEqual(result, (200, {"ok": True, "type": "fill", "kept": ["claim"]}))
        self.assertEqual((self.fake.gets, self.fake.puts), (2, 2))
        text = self.fake.text(CARD)
        self.assertIn("## 主張\n\n先の主張\n\n## 根拠", text)
        self.assertIn("## なぜ自分に重要か\n\nQ\n\n## 使う場面\n\n- R\n", text)
        self.assertEqual(text.count("| 補完 | - | - | 一部記入(web)"), 1)

    def test_B6_fill_conflict_twice(self):
        self.fake.add(CARD, F.B)
        self.fake.before_put = lambda fake, n: fake.add(CARD, fake.text(CARD))
        self.assertError(self.fill(), 409, "save_conflict")

    def test_B7_B8_put_422_is_not_retried(self):
        for msg in ('"sha" wasn\'t supplied', "Invalid request"):
            with self.subTest(msg=msg):
                self.fake.add(CARD, F.A)
                self.fake.calls.clear()
                self.fake.put_errors = {1: http_error(422, msg)}
                status, resp = self.graded()
                self.assertEqual(status, 500)
                self.assertFalse(resp["ok"])
                self.assertEqual((self.fake.gets, self.fake.puts), (1, 1))
                self.assertEqual(self.sleeps, [])

    def test_B9_put_500_or_urlerror_is_not_retried(self):
        for err in (http_error(500, "Server Error"), urllib.error.URLError("timed out")):
            with self.subTest(err=type(err).__name__):
                self.fake.add(CARD, F.A)
                self.fake.calls.clear()
                self.fake.put_errors = {1: err}
                status, resp = self.graded()
                self.assertEqual(status, 500)
                self.assertFalse(resp["ok"])
                self.assertEqual(self.fake.puts, 1)
                self.assertEqual(self.sleeps, [])

    def test_B9b_failures_while_rereading(self):
        """review-v4（Codex）：1回目の PUT が 409 のあと、取り直しが失敗する各ケース。"""
        broken = "frontmatter のない本文\n"
        note = F.A.replace("type: card", "type: note")
        cases = (
            ("GET 500", {2: http_error(500, "Server Error")}, None, 500, None),
            ("GET URLError", {2: urllib.error.URLError("timed out")}, None, 500, None),
            ("frontmatter 不正", {}, broken, 404, "card_not_found"),
            ("type 不一致", {}, note, 404, "card_not_found"),
        )
        for label, get_errors, replacement, exp_status, exp_code in cases:
            with self.subTest(label):
                self.fake.add(CARD, F.A)
                self.fake.calls.clear()
                self.claude_calls.clear()
                self.fake.get_errors = get_errors

                def interrupt(fake, n, replacement=replacement):
                    if n == 1:
                        fake.add(CARD, replacement if replacement is not None else fake.text(CARD) + "\n割り込み")
                self.fake.before_put = interrupt
                status, resp = self.graded()
                self.assertEqual(status, exp_status)
                self.assertFalse(resp["ok"])
                if exp_code:
                    self.assertEqual(resp, {"ok": False, "error": exp_code})
                self.assertNotIn("type", resp)
                self.assertEqual((len(self.claude_calls), self.fake.gets, self.fake.puts), (1, 2, 1))

    def test_B10_card_deleted_before_reread(self):
        self.fake.add(CARD, F.A)

        def delete(fake, n):
            if n == 1:
                del fake.files[CARD]
        self.fake.before_put = delete
        self.assertError(self.graded(), 404, "card_not_found")
        self.assertEqual(self.fake.puts, 1)

    def test_B11_wait_only_on_conflict(self):
        self.assertEqual(grade.RETRY_WAIT_SEC, 0)  # patched
        self.fake.add(CARD, F.A)
        self.graded()
        self.assertEqual(self.sleeps, [])
        self.fake.calls.clear()
        self.fake.before_put = lambda fake, n: n == 1 and fake.add(CARD, fake.text(CARD) + "\n割り込み")
        self.graded()
        self.assertEqual(self.sleeps, [0])

    def test_B11_default_wait_is_one_second(self):
        self.assertEqual(DEFAULT_RETRY_WAIT_SEC, 1.0)

    def test_B12_date_fixed_once_per_request(self):
        self.today_seq = [TODAY, date(2026, 10, 3)]
        self.fake.add(CARD, F.A)
        self.fake.before_put = lambda fake, n: n == 1 and fake.add(CARD, fake.text(CARD) + "\n割り込み")
        status, _ = self.graded(date=None)
        self.assertEqual(status, 200)
        self.assertEqual(self.today_calls, 1)
        text = self.fake.text(CARD)
        self.assertIn("last_reviewed: 2026-10-02\n", text)
        self.assertIn("- 2026-10-02 | 適用 |", text)
        self.assertNotIn("2026-10-03 |", text)
        self.assertEqual(self.fake.messages, ["recall(web): update card state for 2026-10-02"])


# ====================================================================== C
class TestValidation(GradeTestBase):
    def assert400(self, result, code):
        self.assertError(result, 400, code)
        self.assertNoExternal()

    def ok_payload(self, **kw):
        p = {"date": "2026-10-02", "card": CARD, "qtype": "apply", "question": "問い", "answers": {"text": "回答"}}
        p.update(kw)
        return p

    def test_C1_content_length_invalid(self):
        body = json.dumps(self.ok_payload()).encode()
        for cl in (None, "", "abc", "-1", "+5", "1.5", "²", "１２", "1 2"):
            with self.subTest(cl=cl):
                self.assert400(grade.handle_request(cl, io.BytesIO(body)), "invalid_content_length")

    def test_C1b_content_length_with_spaces_passes(self):
        body = b'{"card": "a.md"}'
        body = body + b" " * (123 - len(body))
        for cl in (" 123 ", "\t123"):
            with self.subTest(cl=cl):
                self.assert400(grade.handle_request(cl, io.BytesIO(body)), "invalid_qtype")

    def test_C2_empty_body(self):
        self.assert400(grade.handle_request("0", io.BytesIO(b"")), "empty_body")

    def test_C3_body_too_large_is_not_read(self):
        rfile = mock.Mock()
        rfile.read.side_effect = AssertionError("must not read")
        self.assert400(grade.handle_request("65537", rfile), "body_too_large")
        rfile.read.assert_not_called()

    def test_C4_body_at_limit(self):
        self.fake.add(CARD, F.A)
        data = json.dumps(self.ok_payload()).encode()
        data += b" " * (65536 - len(data))
        self.assertEqual(len(data), 65536)
        status, resp = self.post_raw(data)
        self.assertEqual((status, resp["ok"]), (200, True))

    def test_C5_C6_invalid_json(self):
        for data in (b"\xff\xfe", b'{"card":', b"[" * 2000 + b"0" + b"]" * 2000,
                     b'{"a":' * 2000 + b"0" + b"}" * 2000):
            with self.subTest(data=data[:10]):
                self.assert400(self.post_raw(data), "invalid_json")

    def test_C6c_huge_number(self):
        status, resp = self.post_raw(b"9" * 5000)
        self.assertEqual(status, 400)
        self.assertIn(resp["error"], ("invalid_json", "invalid_payload"))

    def test_C7_not_an_object(self):
        for data in (b"[]", b'"x"', b"1", b"null"):
            with self.subTest(data=data):
                self.assert400(self.post_raw(data), "invalid_payload")

    def test_C8_invalid_card(self):
        names = [123, None, "x.txt", ".md", "../x.md", "a/b.md", "a\\b.md", ".hidden.md", "a\nb.md",
                 "a\u0000.md", "a\u007f.md", "   .md", "　.md", "a" * 253 + ".md"]
        for name in names:
            with self.subTest(name=repr(name)[:30]):
                self.assert400(self.post(self.ok_payload(card=name)), "invalid_card")
        p = self.ok_payload()
        del p["card"]
        self.assert400(self.post(p), "invalid_card")

    def test_C9_existing_card_names_pass(self):
        names = ["「合ってますか？」は答えを上司に預けること.md", "判断はP／LではなくB／Sで考える.md",
                 "理解の3要素：説明できる・即座に使える・応用できる.md", "あ" * 84 + ".md"]
        self.assertEqual(len(names[-1].encode("utf-8")), 255)
        for name in names:
            with self.subTest(name=name[:10]):
                self.fake.add(name, F.A)
                status, resp = self.graded(card=name)
                self.assertEqual((status, resp["ok"]), (200, True))
                self.assertEqual(self.fake.calls[-1], ("PUT", name))

    def test_C10_invalid_qtype(self):
        for q in (None, "", "Recall", "graded", 1, True, [], {}, ["recall"], "\ud800"):
            with self.subTest(q=repr(q)):
                self.assert400(self.post(self.ok_payload(qtype=q), ascii_json=True), "invalid_qtype")
        p = self.ok_payload()
        del p["qtype"]
        self.assert400(self.post(p), "invalid_qtype")

    def test_C11_invalid_answers(self):
        for a in (None, [], "text"):
            with self.subTest(a=repr(a)):
                self.assert400(self.post(self.ok_payload(answers=a)), "invalid_answers")
        p = self.ok_payload()
        del p["answers"]
        self.assert400(self.post(p), "invalid_answers")

    def test_C12_answer_text_not_string(self):
        for t in (123, ["a"], {}):
            with self.subTest(t=repr(t)):
                self.assert400(self.post(self.ok_payload(answers={"text": t})), "invalid_answers")

    def test_C13_answer_too_long(self):
        self.assert400(self.post(self.ok_payload(answers={"text": "あ" * 4001})), "answer_too_long")
        self.fake.add(CARD, F.A)
        self.assertEqual(self.post(self.ok_payload(answers={"text": "あ" * 4000}))[0], 200)

    def test_C14_empty_answer(self):
        for answers in ({"text": ""}, {"text": "   "}, {}, {"text": None}):
            with self.subTest(answers=answers):
                self.assertEqual(self.post(self.ok_payload(answers=answers)),
                                 (200, {"ok": False, "error": "empty_answer"}))
                self.assertNoExternal()

    def test_C15_fill_not_string(self):
        p = self.ok_payload(qtype="fill", answers={"claim": 5, "why": "Q", "scene": "R"})
        self.assert400(self.post(p), "invalid_answers")

    def test_C16_fill_too_long(self):
        p = self.ok_payload(qtype="fill", answers={"claim": "P", "why": "Q", "scene": "あ" * 1001})
        self.assert400(self.post(p), "answer_too_long")
        for scene in ("あ" * 1000, ("あ" * 9 + "\n") * 100):
            with self.subTest(n=len(scene)):
                self.fake.add(CARD, F.B)
                p = self.ok_payload(qtype="fill", answers={"claim": "P", "why": "Q", "scene": scene})
                self.assertEqual(self.post(p), (200, {"ok": True, "type": "fill"}))

    def test_C17_fill_incomplete(self):
        for field in ("claim", "why", "scene"):
            for v in ("", None, " \n \n", "missing"):
                with self.subTest(field=field, v=v):
                    answers = dict(FILL_PQR)
                    if v == "missing":
                        del answers[field]
                    else:
                        answers[field] = v
                    self.assertEqual(self.post(self.ok_payload(qtype="fill", answers=answers)),
                                     (200, {"ok": False, "error": "incomplete_answer"}))
                    self.assertNoExternal()

    def test_C18_invalid_question(self):
        for q in (123, "あ" * 2001):
            with self.subTest(q=str(q)[:5]):
                self.assert400(self.post(self.ok_payload(question=q)), "invalid_question")

    def test_C19_invalid_date(self):
        for d in ("2026/10/02", "2026-13-01", "2026-02-30", "2026-10-02\nx", 20261002, "２０２６-１０-０２"):
            with self.subTest(d=repr(d)):
                self.assert400(self.post(self.ok_payload(date=d)), "invalid_date")

    def test_C20_date_defaults_to_today(self):
        self.fake.add(CARD, F.A)
        p = self.ok_payload()
        del p["date"]
        self.assertEqual(self.post(p)[0], 200)
        self.assertEqual(self.fake.messages, ["recall(web): update card state for 2026-10-02"])

    def test_C21_test_flag(self):
        for t in ("true", 1):
            with self.subTest(t=t):
                self.assert400(self.post(self.ok_payload(test=t)), "invalid_test")
        results = []
        for t in (True, False, "absent"):
            self.fake.add(CARD, F.A)
            p = self.ok_payload() if t == "absent" else self.ok_payload(test=t)
            results.append(self.post(p))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(results[0][0], 200)

    def test_C22_unknown_keys_are_ignored(self):
        self.fake.add(CARD, F.A)
        self.assertEqual(self.post(self.ok_payload(extra="x", nested={"a": 1}))[0], 200)

    def test_C23_first_problem_wins(self):
        self.assert400(self.post(self.ok_payload(card="../x.md", qtype="nope")), "invalid_card")

    def test_C24_card_not_on_github(self):
        self.assertError(self.graded(), 404, "card_not_found")
        self.assertEqual((len(self.claude_calls), self.fake.puts), (0, 0))

    def test_C25_not_a_card(self):
        for text in ("frontmatter のない本文\n", F.A.replace("type: card", "type: note")):
            with self.subTest(text=text[:10]):
                self.fake.add(CARD, text)
                self.fake.calls.clear()
                self.assertError(self.graded(), 404, "card_not_found")
                self.assertEqual((self.fake.puts, len(self.claude_calls)), (0, 0))

    def test_C26_cors_on_every_status(self):
        def run(payload, setup=None):
            if setup:
                setup()
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            h = grade.handler.__new__(grade.handler)
            h.headers = {"Content-Length": str(len(data))}
            h.rfile = io.BytesIO(data)
            h.wfile = io.BytesIO()
            h.request_version = "HTTP/1.1"
            h.requestline = "POST /api/grade HTTP/1.1"
            h.command = "POST"
            h.client_address = ("127.0.0.1", 0)
            h.log_request = lambda *a, **k: None
            h.do_POST()
            raw = h.wfile.getvalue().decode("utf-8")
            head, _, body = raw.partition("\r\n\r\n")
            return int(head.split()[1]), head, json.loads(body)

        def conflict():
            self.fake.add(CARD, F.C)

        def server_error():
            self.fake.add(CARD, F.A)
            self.fake.put_errors = {1: urllib.error.URLError("timed out")}

        cases = [
            (200, self.ok_payload(qtype="fill", answers={}), None),
            (400, self.ok_payload(card="../x.md"), None),
            (404, self.ok_payload(card="ない.md"), None),
            (409, self.ok_payload(qtype="fill", answers=dict(FILL_PQR)), conflict),
            (500, self.ok_payload(), server_error),
        ]
        for exp, payload, setup in cases:
            with self.subTest(status=exp):
                status, head, body = run(payload, setup)
                self.assertEqual(status, exp)
                self.assertIn("Access-Control-Allow-Origin: https://horiken1977.github.io", head)
                self.assertIn("Content-Type: application/json", head)
                self.assertIn("ok", body)

    def test_C27_card_lone_surrogate(self):
        for name in ("\ud800.md", "会議\udfff.md"):
            with self.subTest(name=repr(name)):
                self.assert400(self.post(self.ok_payload(card=name), ascii_json=True), "invalid_unicode")

    def test_C28_answer_lone_surrogate(self):
        for t in ("\ud800", "回答\udc00です"):
            with self.subTest(t=repr(t)):
                self.assert400(self.post(self.ok_payload(answers={"text": t}), ascii_json=True), "invalid_unicode")

    def test_C29_fill_lone_surrogate(self):
        for field in ("claim", "why", "scene"):
            with self.subTest(field=field):
                answers = dict(FILL_PQR)
                answers[field] = "\ud800"
                self.assert400(self.post(self.ok_payload(qtype="fill", answers=answers), ascii_json=True),
                               "invalid_unicode")

    def test_C30_question_date_lone_surrogate(self):
        self.assert400(self.post(self.ok_payload(question="\ud800"), ascii_json=True), "invalid_unicode")
        self.assert400(self.post(self.ok_payload(date="\ud800"), ascii_json=True), "invalid_unicode")
        self.fake.add(CARD, F.B)
        answers = dict(FILL_PQR, text="\ud800")
        self.assertEqual(self.post(self.ok_payload(qtype="fill", answers=answers), ascii_json=True),
                         (200, {"ok": True, "type": "fill"}))

    def test_C31_emoji(self):
        for ascii_json in (False, True):
            with self.subTest(ascii_json=ascii_json):
                self.fake.add(CARD, F.A)
                status, _ = self.post(self.ok_payload(answers={"text": "😀"}), ascii_json=ascii_json)
                self.assertEqual(status, 200)
                self.assertIn("  - 回答: \U0001F600\n", self.fake.text(CARD))
        name = "😀テスト.md"
        self.fake.add(name, F.A)
        self.assertEqual(self.post(self.ok_payload(card=name, question="問い😀"), ascii_json=True)[0], 200)
        self.fake.add(CARD, F.B)
        self.assertEqual(self.post(self.ok_payload(qtype="fill", answers={"claim": "😀", "why": "😀", "scene": "😀"})),
                         (200, {"ok": True, "type": "fill"}))

    def test_C32_html_entity_of_surrogate(self):
        self.fake.add(CARD, F.A)
        status, _ = self.post(self.ok_payload(answers={"text": "&#xD800;"}))
        self.assertEqual(status, 200)
        text = self.fake.text(CARD)
        self.assertIn("  - 回答: \ufffd\n", text)
        text.encode("utf-8")


# ====================================================================== D
def old_lib_is_unfilled(body):
    """変更前の lib.is_unfilled（本文全体の文字列検索）。v2 の重大1の再現の確認用。"""
    return "（未記入" in body or "（AIの下書き" in body


V3_OLD_SCENE = re.compile(r"- (.+)" + re.escape(F.OLD_SUFFIX))


def headings(text):
    return re.findall(r"^## (.+)$", text, re.M)


class TestPendingSections(unittest.TestCase):
    def test_D1_fixtures(self):
        for key, text in F.ALL.items():
            with self.subTest(card=key):
                self.assertEqual(grade.pending_sections(F.body_of(text)), F.EXPECTED_PENDING[key])
        for key in ("c†", "d†"):
            scene_line = "- " + F.SCENE_DAGGER
            self.assertIn("\n" + scene_line + "\n", F.ALL[key])
            self.assertTrue(V3_OLD_SCENE.fullmatch(scene_line))  # v3 はこれを未記入と誤った
        for key in ("c*", "d*"):
            self.assertTrue(old_lib_is_unfilled(F.body_of(F.ALL[key])))  # 変更前の lib はこれを未記入と誤った

    def test_D2_strings_not_counted(self):
        for label, text in F.d2_bodies().items():
            with self.subTest(label):
                self.assertEqual(grade.pending_sections(F.body_of(text)), set())

    def test_D2_mark_lines_with_spaces_are_counted(self):
        text = F.make_card(claim="X", why="  " + F.WHY_MARK + "  ", scene="- Z")
        self.assertEqual(grade.pending_sections(F.body_of(text)), {"why"})
        for line in sorted(F.OLD_LINES):
            with self.subTest(line=line):
                text = F.make_card(claim="X", why="Y", scene="  " + line + " \t")
                self.assertEqual(grade.pending_sections(F.body_of(text)), {"scene"})

    def test_section_span(self):
        body = "\n## 主張\n\nA\n\n### 小見出し\n\n## 根拠\n\nB"
        s, e = grade.section_span(body, "主張")
        self.assertEqual(body[s:e], "\nA\n\n### 小見出し\n\n")
        s, e = grade.section_span(body, "根拠")
        self.assertEqual(body[s:e], "\nB")
        self.assertIsNone(grade.section_span(body, "使う場面"))
        self.assertEqual(grade.section_span("## 主張", "主張"), (len("## 主張"), len("## 主張")))


class TestFillWriting(GradeTestBase):
    def run_fill(self, src, answers):
        self.fake.add(CARD, src)
        self.fake.calls.clear()
        return self.fill(answers)

    def test_D3_review_reproduction_v2(self):
        result = self.run_fill(F.E, {"claim": "遅れた主張", "why": F.Y_STAR, "scene": "読書したとき"})
        self.assertEqual(result, (200, {"ok": True, "type": "fill", "kept": ["claim"]}))
        text = self.fake.text(CARD)
        self.assertIn("## 主張\n\n先の主張\n\n## 根拠", text)
        self.assertEqual(grade.pending_sections(F.body_of(text)), set())
        self.assertFalse(lib.is_unfilled(F.parse(text)))
        saved = self.snapshot()
        self.fake.calls.clear()
        self.assertError(self.fill({"claim": "さらに遅れた主張", "why": "W", "scene": "S"}), 409, "already_filled")
        self.assertEqual(self.fake.puts, 0)
        self.assertEqual(self.snapshot(), saved)

    def test_D4_partly_filled(self):
        before_claim = F.body_of(F.E).split("## 根拠")[0]
        result = self.run_fill(F.E, FILL_PQR)
        self.assertEqual(result, (200, {"ok": True, "type": "fill", "kept": ["claim"]}))
        text = self.fake.text(CARD)
        self.assertEqual(F.body_of(text).split("## 根拠")[0], before_claim)
        self.assertIn("## なぜ自分に重要か\n\nQ\n\n## 使う場面\n\n- R\n", text)
        self.assertEqual(text.count("一部記入(web)"), 1)
        self.assertIn("status: 要復習\n", text)
        self.assertIn("next_review: 2026-10-03\n", text)

    def test_D5_advanced_status_is_kept(self):
        fm_before = F.E2.split("\n---\n", 1)[0]
        self.assertEqual(self.run_fill(F.E2, FILL_PQR)[0], 200)
        text = self.fake.text(CARD)
        self.assertEqual(text.split("\n---\n", 1)[0], fm_before)
        self.assertIn("## なぜ自分に重要か\n\nQ\n\n## 使う場面\n\n- R\n", text)
        self.assertEqual(text.count("| 補完 |"), 1)

    def test_D6_placeholder_answers_are_blank(self):
        cases = [
            {"claim": F.CLAIM_MARK}, {"why": F.WHY_MARK}, {"scene": F.SCENE_MARK},
            {"scene": "部下・チーム" + F.OLD_SUFFIX}, {"scene": "顧客提案・商談" + F.OLD_SUFFIX},
            {"scene": "自分自身" + F.OLD_SUFFIX}, {"why": "\n  " + F.WHY_MARK + "\n"},
        ]
        for change in cases:
            with self.subTest(change=change):
                self.fake.calls.clear()
                result = self.fill(dict(FILL_PQR, **change))
                self.assertEqual(result, (200, {"ok": False, "error": "incomplete_answer"}))
                self.assertNoExternal()

    D6_SAVED = [
        {"claim": F.X_STAR, "why": F.Y_STAR, "scene": F.Z_STAR},
        {"why": "前置き" + F.WHY_MARK}, {"scene": F.SCENE_MARK + "のとき"}, {"claim": F.WHY_MARK},
        {"scene": F.SCENE_DAGGER}, {"scene": "部下" + F.OLD_SUFFIX}, {"scene": "その他" + F.OLD_SUFFIX},
        {"scene": F.OLD_SUFFIX}, {"scene": "部下・チーム" + F.OLD_SUFFIX + "のとき"},
    ]

    def test_D6_answers_containing_marks_are_saved(self):
        for change in self.D6_SAVED:
            with self.subTest(change=change):
                answers = dict(FILL_PQR, **change)
                self.assertEqual(self.run_fill(F.B, answers), (200, {"ok": True, "type": "fill"}))
                text = self.fake.text(CARD)
                self.assertIn("## 主張\n\n" + answers["claim"] + "\n\n## 根拠", text)
                self.assertIn("## なぜ自分に重要か\n\n" + answers["why"] + "\n\n", text)
                self.assertIn("## 使う場面\n\n- " + answers["scene"] + "\n\n", text)

    def test_D7_normalization_and_special_characters(self):
        self.run_fill(F.B, dict(FILL_PQR, why="一行目\n\n二行目"))
        self.assertIn("## なぜ自分に重要か\n\n一行目 / 二行目\n\n", self.fake.text(CARD))

        self.run_fill(F.B, dict(FILL_PQR, claim="## 根拠"))
        text = self.fake.text(CARD)
        self.assertIn("## 主張\n\n\\## 根拠\n\n## 根拠", text)
        self.assertEqual(headings(text), ["主張", "根拠", "なぜ自分に重要か", "使う場面", "回答履歴"])

        for answers in ({"claim": "C:\\data を見る"}, {"why": "\\1 の参照"}, {"why": "改行\\nのつもり"},
                        {"scene": "\\g<0> と \\\\"}):
            with self.subTest(answers=answers):
                a = dict(FILL_PQR, **answers)
                self.assertEqual(self.run_fill(F.B, a), (200, {"ok": True, "type": "fill"}))
                text = self.fake.text(CARD)
                self.assertIn("\n\n" + a["claim"] + "\n\n## 根拠", text)
                self.assertIn("\n\n" + a["why"] + "\n\n## 使う場面", text)
                self.assertIn("\n\n- " + a["scene"] + "\n\n## 回答履歴", text)

        self.run_fill(F.B, dict(FILL_PQR, scene="#タグ"))
        self.assertIn("## 使う場面\n\n- #タグ\n\n", self.fake.text(CARD))

    def test_D8_postcondition(self):
        answer_sets = [dict(FILL_PQR)] + [dict(FILL_PQR, **c) for c in self.D6_SAVED] + [
            dict(FILL_PQR, why="一行目\n\n二行目"), dict(FILL_PQR, claim="## 根拠"),
            dict(FILL_PQR, claim="C:\\data"), dict(FILL_PQR, scene="#タグ"),
        ]
        for key in ("b", "b2", "b3", "e", "e2"):
            for i, answers in enumerate(answer_sets):
                with self.subTest(card=key, answers=i):
                    self.assertEqual(self.run_fill(F.ALL[key], answers)[0], 200)
                    text = self.fake.text(CARD)
                    self.assertEqual(grade.pending_sections(F.body_of(text)), set())
                    self.assertEqual(headings(text), ["主張", "根拠", "なぜ自分に重要か", "使う場面", "回答履歴"])

    def test_D8_postcondition_guard(self):
        """事後条件が破れたら書かずに 500（設計どおりなら起きない。守りが効くことだけ確かめる）。"""
        self.fake.add(CARD, F.B)
        with mock.patch.object(grade, "_replace_mark_lines", lambda text, field, v: text):
            status, resp = self.fill()
        self.assertEqual(status, 500)
        self.assertEqual(self.fake.puts, 0)

    def test_D9_old_form_scene(self):
        for src, exp in ((F.B2, "- 部下・チーム：R"), (F.B3, "- 自分自身：R")):
            with self.subTest(exp=exp):
                self.run_fill(src, FILL_PQR)
                self.assertIn("## 使う場面\n\n" + exp + "\n\n", self.fake.text(CARD))
        two = F.make_card(scene="- 部下・チーム" + F.OLD_SUFFIX + "\n- 顧客提案・商談" + F.OLD_SUFFIX)
        self.run_fill(two, FILL_PQR)
        self.assertIn("## 使う場面\n\n- 部下・チーム：R\n- 顧客提案・商談：R\n\n", self.fake.text(CARD))

    def test_D9b_old_form_line_with_surrounding_spaces(self):
        """review-v4（Gemini）：strip して照合し、行頭の字下げと前後の構造を保って置き換える。"""
        src = F.make_card(why="  " + F.WHY_MARK + " ",
                          scene="  - 部下・チーム" + F.OLD_SUFFIX + "  \n\t- 自分自身" + F.OLD_SUFFIX)
        self.assertEqual(self.run_fill(src, FILL_PQR), (200, {"ok": True, "type": "fill"}))
        text = self.fake.text(CARD)
        self.assertIn("## なぜ自分に重要か\n\n  Q \n\n", text)
        self.assertIn("## 使う場面\n\n  - 部下・チーム：R  \n\t- 自分自身：R\n\n## 回答履歴", text)
        self.assertEqual(grade.pending_sections(F.body_of(text)), set())
        src = F.make_card(scene="   - " + F.SCENE_MARK)
        self.run_fill(src, FILL_PQR)
        self.assertIn("## 使う場面\n\n   - R\n\n", self.fake.text(CARD))

    def test_D10_only_target_sections_are_rewritten(self):
        evidence = F.EVIDENCE + "\n\n- 自分のメモ（location 3）：" + F.WHY_MARK + "の欄を早く埋める"
        history = ("\n- 2026-09-30 | 想起 | 4/8 | 不合格 | 一言"
                   "\n  - 回答: " + F.WHY_MARK + " と " + F.SCENE_MARK +
                   "\n  - 模範解答例: " + F.CLAIM_MARK)
        src = F.make_card(evidence=evidence, history=history)
        self.run_fill(src, FILL_PQR)
        text = self.fake.text(CARD)
        body = F.body_of(text)
        s, e = grade.section_span(body, "根拠")
        self.assertEqual(body[s:e], "\n" + evidence + "\n\n")
        self.assertTrue(text.endswith(history))

    def test_D11_v3_reproduction(self):
        for src, scene, line in (
            (F.B, F.SCENE_DAGGER, "- " + F.SCENE_DAGGER),
            (F.B2, F.SCENE_DDAGGER, "- 部下・チーム：" + F.SCENE_DDAGGER),
        ):
            with self.subTest(line=line[:20]):
                result = self.run_fill(src, {"claim": "X", "why": "Y", "scene": scene})
                self.assertEqual(result, (200, {"ok": True, "type": "fill"}))
                text = self.fake.text(CARD)
                self.assertIn("## 使う場面\n\n" + line + "\n\n", text)
                self.assertEqual(grade.pending_sections(F.body_of(text)), set())
                self.assertFalse(lib.is_unfilled(F.parse(text)))
                saved = self.snapshot()
                self.fake.calls.clear()
                self.assertError(self.fill(FILL_PQR), 409, "already_filled")
                self.assertEqual(self.fake.puts, 0)
                self.assertEqual(self.snapshot(), saved)


class TestPureHelpers(unittest.TestCase):
    def test_normalize_fill_answer(self):
        n = grade.normalize_fill_answer
        self.assertEqual(n("why", "  a \n\n b  "), "a / b")
        self.assertEqual(n("claim", "# x"), "\\# x")
        self.assertEqual(n("why", "## x"), "\\## x")
        self.assertEqual(n("scene", "# x"), "# x")
        self.assertEqual(n("why", " \n "), "")

    def test_api_error_extra_none(self):
        e = grade.ApiError(409, "save_conflict", extra=None)
        self.assertEqual(e.extra, {})
        self.assertEqual({"ok": False, "error": e.code, **e.extra}, {"ok": False, "error": "save_conflict"})

    def test_unsaved_of(self):
        resp = {"ok": True, "type": "graded", "score": {"total": 6}, "passed": True, "feedback": "f",
                "model_answer": "m", "new_status": "要確認", "next_review": "2026-10-09", "advanced": True}
        self.assertEqual(grade.unsaved_of(resp), {"unsaved": {"type": "graded", "score": {"total": 6},
                                                              "passed": True, "feedback": "f", "model_answer": "m"}})
        self.assertEqual(grade.unsaved_of({"ok": True, "type": "fill"}), {})


if __name__ == "__main__":
    unittest.main()
