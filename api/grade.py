"""Vercel serverless function: grade a recall answer and update the card
file in the GitHub repo via the Contents API. Secrets (ANTHROPIC_API_KEY,
GITHUB_PAT) live only in Vercel environment variables, never in any
GitHub-tracked file, so GitHub's secret scanning never sees them."""
import base64
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

REPO = "horiken1977/mybrain-cards"
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"
ALLOWED_ORIGIN = "https://horiken1977.github.io"

JST = timezone(timedelta(hours=9))
FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
STATUS_ORDER = ["未履修", "要復習", "学習中", "要確認", "安定"]
INTERVAL_DAYS = {"要復習": 1, "学習中": 3, "要確認": 7, "安定": 30}
QTYPE_LABEL = {"recall": "想起", "apply": "適用", "contrast": "対比・接続", "refute": "反証", "fill": "補完"}

# 入力の上限（GitHub・Claude を呼ぶ前に検査する）
MAX_BODY_BYTES = 65536
MAX_ANSWER_CHARS = 4000
MAX_FILL_CHARS = 1000
MAX_QUESTION_CHARS = 2000
MAX_CARD_BYTES = 255
# 保存が sha 不一致（409）で失敗したとき、取り直す前に待つ秒数（直後の GET が古い sha を返すことがあるため）
RETRY_WAIT_SEC = 1.0
# 1日の採点・補完の上限（2026-10-09 の方式 (b)、ToDo B-4）。件数は cards リポジトリの api_usage.json に書く
DAILY_LIMIT = 30
USAGE_FILE = "api_usage.json"
DAILY_LIMIT_MESSAGE = "今日の採点はここまで。明日また"

# 補完の対象の節と、テンプレート（kindle_update.render_card・/card）が書く未記入の目印の行。
# scripts/lib.py にも同じものを置く（Vercel の関数は api/ だけで動くので import しない。tests でずれを防ぐ）
FIELD_SECTION = {"claim": "主張", "why": "なぜ自分に重要か", "scene": "使う場面"}
CLAIM_MARK = "（AIの下書き。初回の /recall で自分の言葉に直す）"
WHY_MARK = "（未記入：初回の /recall で自分の言葉で1行書く）"
SCENE_MARK = "（未記入：使う場面を初回の補完で書く）"
# /card の古い形の使う場面（もう作られない）。全履歴で使われた分類はこの3つだけ
OLD_SCENE_SUFFIX = "（具体的な状況は初回の /recall で追記）"
OLD_SCENE_CATEGORIES = ("部下・チーム", "顧客提案・商談", "自分自身")
OLD_SCENE_LINES = frozenset("- " + c + OLD_SCENE_SUFFIX for c in OLD_SCENE_CATEGORIES)
HEADING_RE = re.compile(r"^## (.+)$", re.M)


def today_jst():
    return datetime.now(JST).date()


def parse_scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def parse_card(text):
    m = FM_RE.match(text)
    if not m:
        return None
    fm_text, body = m.group(1), m.group(2)
    fm = {}
    for line in fm_text.split("\n"):
        if not line.strip() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        fm[key.strip()] = parse_scalar(val)
    return {"fm": fm, "body": body}


def write_frontmatter(fm, body, updates):
    fm = dict(fm)
    fm.update(updates)
    lines = ["---"]
    for key in ["title", "type", "book", "status", "priority", "last_reviewed", "next_review", "streak", "tags"]:
        val = fm.get(key, "")
        if key == "book":
            lines.append(f'book: "{val}"' if val else "book:")
        elif key == "tags":
            lines.append(f"tags: {val}" if val.startswith("[") else f"tags: [{val}]")
        else:
            lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


# 回答履歴の1行目（`- 日付 | 型 | 点数 | 合否 | 一言`）。行頭固定なので字下げしたサブ項目は拾わない。
# 新しい行ほど上にあるので、findall の先頭が最新
HIST_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) \| ([^|\n]+?) \| ([^|\n]*?) \| ([^|\n]*?) \|", re.M)


def sanitize(text):
    """Keep a value on one history line: no pipes (column separator) or newlines."""
    text = html.unescape(str(text or ""))
    text = text.replace("|", "｜")
    return " / ".join(part.strip() for part in text.splitlines() if part.strip())


def append_history(body, date, qtype_label, score, verdict, note, details=None):
    """Insert a history entry at the top of 回答履歴. `details` is a list of
    (label, text) pairs written as indented sub-items under the entry line."""
    marker = "## 回答履歴\n"
    idx = body.rfind(marker)
    line = f"\n- {date} | {qtype_label} | {score} | {verdict} | {sanitize(note)}"
    for label, text in details or []:
        if sanitize(text):
            line += f"\n  - {label}: {sanitize(text)}"
    if idx == -1:
        return body.rstrip("\n") + f"\n\n{marker}{line}\n"
    insert_at = idx + len(marker)
    return body[:insert_at] + line + body[insert_at:]


def advance_status(status):
    i = STATUS_ORDER.index(status) if status in STATUS_ORDER else 0
    return STATUS_ORDER[min(i + 1, len(STATUS_ORDER) - 1)]


def regress_status(status):
    i = STATUS_ORDER.index(status) if status in STATUS_ORDER else 0
    return STATUS_ORDER[max(i - 1, 0)]


def next_review_after_pass(new_status, streak, today):
    days = INTERVAL_DAYS.get(new_status, 1)
    if new_status == "安定" and streak >= 1:
        days = 60 if streak > 1 else 30
    return (today + timedelta(days=days)).isoformat()


def get_claim(body):
    m = re.search(r"## 主張\n\n(.+?)\n\n", body, re.DOTALL)
    return m.group(1).strip() if m else ""


def get_evidence(body):
    m = re.search(r"## 根拠\n\n(.+?)\n\n(?:原本|##)", body, re.DOTALL)
    return m.group(1).strip() if m else ""


def gh_request(method, path, body=None):
    url = f"https://api.github.com/repos/{REPO}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {GITHUB_PAT}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


class ApiError(Exception):
    """An error answered as {"ok": false, "error": code, **extra} with the given HTTP status."""

    def __init__(self, status, code, extra=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = dict(extra) if extra else {}


class SaveConflict(Exception):
    """The PUT was rejected with 409: the card's sha changed since it was read."""


def load_card(filename):
    """GET the card and return {"fm", "body", "sha"}. A missing file, or one that is
    not a readable `type: card`, is card_not_found (404)."""
    try:
        data = gh_request("GET", f"contents/{urllib.parse.quote(filename, safe='')}?ref=main")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ApiError(404, "card_not_found") from None
        raise
    try:
        content = base64.b64decode(data["content"]).decode("utf-8")
    except ValueError:
        raise ApiError(404, "card_not_found") from None
    parsed = parse_card(content)
    if parsed is None or parsed["fm"].get("type") != "card":
        raise ApiError(404, "card_not_found")
    parsed["sha"] = data["sha"]
    return parsed


def save_card(filename, new_text, sha, date):
    body = {
        "message": f"recall(web): update card state for {date}",
        "content": base64.b64encode(new_text.encode("utf-8")).decode("ascii"),
        "sha": sha,
        "branch": "main",
        "committer": {"name": "recall-bot", "email": "actions@users.noreply.github.com"},
    }
    try:
        gh_request("PUT", f"contents/{urllib.parse.quote(filename, safe='')}", body)
    except urllib.error.HTTPError as e:
        # 409 だけが sha の不一致。422・5xx・タイムアウトは再試行しない（PUT が実は成功していて二重に記録するのを避ける）
        if e.code == 409:
            raise SaveConflict() from None
        raise


def grade_with_claude(title, claim_text, evidence_text, qtype_label, answer):
    prompt = f"""あなたは学習コーチです。以下のカードの主張と、ユーザーの回答を4軸で採点してください。

カード「{title}」
主張: {claim_text}
根拠: {evidence_text}
問いの型: {qtype_label}
ユーザーの回答: {answer}

4軸を各0〜2点で採点してください。あわせて、この問いの型に対する模範解答例を1つ書いてください。
模範解答例は、カードの主張・根拠に沿い、4軸（主張の正確さ／自分の具体例／適用条件と限界／次の行動）をすべて満たす、本人が書いたような一人称の日本語で3〜5文。
模範解答例で本の内容として書くのは、上の「主張」と「根拠」（ハイライトの引用と本人のメモ）にあることだけにする。根拠にない本の記述・エピソード・数字・人名・用語を付け足したり、根拠にないことを本が述べているかのように書いたりしない。
「自分の具体例」の部分だけは、主張が当てはまる場面を例として作ってよい（ただし本の内容としては書かない）。
必ず次のJSON形式のみで返してください（説明文やマークダウンは付けない）：
{{"accuracy": 0-2, "example": 0-2, "conditions": 0-2, "action": 0-2, "feedback": "1〜2文の短いフィードバック（日本語）", "model_answer": "模範解答例（日本語・3〜5文）"}}
"""
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": MODEL, "max_tokens": 800,
                        "messages": [{"role": "user", "content": prompt}]}).encode(),
        method="POST",
    )
    req.add_header("x-api-key", ANTHROPIC_API_KEY)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    text = payload["content"][0]["text"]
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0) if m else text)


def fetch_grade(card, qtype, answer):
    return grade_with_claude(card["fm"].get("title", ""), get_claim(card["body"]),
                             get_evidence(card["body"]), QTYPE_LABEL[qtype], answer)


# ------------------------------------------------------------ 補完（fill）
def section_span(body, name):
    """(start, end) of the first `## <name>` section: from the line after the heading
    to the next `## ` heading (or the end). None if there is no such heading."""
    headings = list(HEADING_RE.finditer(body))
    for i, m in enumerate(headings):
        if m.group(1) == name:
            start = min(m.end() + 1, len(body))
            end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
            return start, end
    return None


def is_mark_line(field, s):
    """`s` is one stripped line. True only for the template's own placeholder lines."""
    if field == "claim":
        return s == CLAIM_MARK
    if field == "why":
        return s == WHY_MARK
    return s == "- " + SCENE_MARK or s in OLD_SCENE_LINES


def pending_sections(body):
    """The fill fields whose section still has a placeholder line."""
    result = set()
    for field, name in FIELD_SECTION.items():
        span = section_span(body, name)
        if span is None:
            continue
        if any(is_mark_line(field, line.strip()) for line in body[span[0]:span[1]].split("\n")):
            result.add(field)
    return result


def is_unfilled(body):
    return bool(pending_sections(body))


def normalize_fill_answer(field, s):
    """One line, joined with ' / '. A claim/why written at the start of a line never
    becomes a heading: a leading '#' is escaped."""
    v = " / ".join(part.strip() for part in s.splitlines() if part.strip())
    if field in ("claim", "why") and v.startswith("#"):
        v = "\\" + v
    return v


def is_placeholder_answer(field, v):
    """True when the (normalized) answer is exactly a template placeholder, i.e. not written."""
    if field == "claim":
        return v == CLAIM_MARK
    if field == "why":
        return v == WHY_MARK
    return v == SCENE_MARK or v in {c + OLD_SCENE_SUFFIX for c in OLD_SCENE_CATEGORIES}


def _replace_mark_lines(text, field, v):
    """Replace only the placeholder lines of `text`, keeping each line's surrounding whitespace."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if not is_mark_line(field, s):
            continue
        lead = line[:len(line) - len(line.lstrip())]
        trail = line[len(line.rstrip()):]
        if field == "why":
            new = v
        elif s in OLD_SCENE_LINES:
            new = s[:-len(OLD_SCENE_SUFFIX)] + "：" + v      # "- <分類>（…）" → "- <分類>：v"
        else:
            new = "- " + v
        lines[i] = lead + new + trail
    return "\n".join(lines)


def apply_fill(fm, body, fill_answers, today):
    """Write the fill answers into the sections that are still unfilled. Filled sections and
    an advanced status are left as they are; with nothing unfilled, already_filled (409)."""
    pending = pending_sections(body)
    if not pending:
        raise ApiError(409, "already_filled")

    new_body = body
    for field in ("scene", "why", "claim"):  # 後ろの節から書き換え、前の節の位置をずらさない
        if field not in pending:
            continue
        start, end = section_span(new_body, FIELD_SECTION[field])
        v = fill_answers[field]
        if field == "claim":
            text = "\n" + v + "\n\n"  # AI の下書きの段落ごと置き換える
        else:
            text = _replace_mark_lines(new_body[start:end], field, v)
        new_body = new_body[:start] + text + new_body[end:]  # re.sub の置換文字列は使わない（\ を解釈させない）

    if pending & pending_sections(new_body):
        raise RuntimeError("fill left a placeholder")

    note = "初回記入(web)" if pending == {"claim", "why", "scene"} else "一部記入(web)"
    new_body = append_history(new_body, today.isoformat(), "補完", "-", "-", note)
    if body.endswith("\n") and not new_body.endswith("\n"):
        new_body += "\n"  # 履歴が空の末尾に挿すと改行が消えるので、元のカードの末尾の改行を残す

    if fm.get("status") in ("学習中", "要確認", "安定"):
        updates = {}  # 進んだ状態を補完で戻さない
    else:
        updates = {"status": "要復習", "last_reviewed": today.isoformat(),
                   "next_review": (today + timedelta(days=1)).isoformat()}
    new_text = write_frontmatter(fm, new_body, updates)

    kept = [f for f in ("claim", "why", "scene") if f not in pending]
    response = {"ok": True, "type": "fill"}
    if kept:
        response["kept"] = kept
    return new_text, response


# ------------------------------------------------------------ 採点
def apply_graded(fm, body, qtype, answer, question, result, today):
    """Apply a grading result (from fetch_grade) to the card."""
    score = result["accuracy"] + result["example"] + result["conditions"] + result["action"]
    passed = score >= 5 and result["accuracy"] >= 1

    status = fm.get("status", "未履修")
    streak = int(fm.get("streak", "0") or 0)

    # 1日1段階まで：今日すでに合格して昇格していれば、2回目以降の合格では状態を進めない
    passed_today = any(d == today.isoformat() and v.strip().startswith("合格")
                       for d, _, _, v in HIST_RE.findall(body))

    if passed:
        if passed_today:
            new_status, new_streak, next_review = status, streak, fm.get("next_review")
        else:
            new_streak = streak + 1
            new_status = advance_status(status)
            next_review = next_review_after_pass(new_status, new_streak, today)
        verdict = "合格"
    else:
        new_status = regress_status(status) if status not in ("未履修", "要復習") else status
        new_streak = max(0, streak - 1)
        next_review = (today + timedelta(days=1)).isoformat()
        verdict = "不合格"

    breakdown = (f"正確さ{result['accuracy']}・具体例{result['example']}・"
                 f"適用条件{result['conditions']}・次の行動{result['action']}")
    body = append_history(body, today.isoformat(), QTYPE_LABEL[qtype], f"{score}/8", verdict,
                          result.get("feedback", ""), details=[
                              ("問い", question),
                              ("回答", answer),
                              ("内訳", breakdown),
                              ("模範解答例", result.get("model_answer", "")),
                          ])
    new_text = write_frontmatter(fm, body, {
        "status": new_status, "last_reviewed": today.isoformat(),
        "next_review": next_review, "streak": str(new_streak),
    })
    response = {
        "ok": True, "type": "graded",
        "score": {"accuracy": result["accuracy"], "example": result["example"],
                  "conditions": result["conditions"], "action": result["action"], "total": score},
        "passed": passed, "feedback": result.get("feedback", ""),
        "model_answer": result.get("model_answer", ""),
        "new_status": new_status, "next_review": next_review,
        "advanced": passed and not passed_today,
    }
    return new_text, response


# ------------------------------------------------------------ 保存と再試行
def unsaved_of(response):
    """What to return with save_conflict: the grading result (not the state, which was not saved)."""
    if response.get("type") == "graded":
        keys = ("type", "score", "passed", "feedback", "model_answer")
        return {"unsaved": {k: response[k] for k in keys}}
    return {}


def commit_with_retry(filename, card, apply_fn, date):
    """Apply and save. On a 409, wait, re-read the card once and re-apply the same result
    (Claude is not called again); a second 409 is save_conflict."""
    new_text, response = apply_fn(card)
    try:
        save_card(filename, new_text, card["sha"], date)
        return response
    except SaveConflict:
        print(f"save conflict on {filename}; retrying once", file=sys.stderr)

    time.sleep(RETRY_WAIT_SEC)
    fresh = load_card(filename)
    new_text, response = apply_fn(fresh)
    try:
        save_card(filename, new_text, fresh["sha"], date)
        return response
    except SaveConflict:
        raise ApiError(409, "save_conflict", extra=unsaved_of(response)) from None


# ------------------------------------------------------------ 入力チェック
ValidRequest = namedtuple("ValidRequest", ["card", "qtype", "answer_text", "fill_answers", "question", "date"])


def _check_unicode(s):
    """Lone surrogates (a JSON escape such as \\ud800) pass json.loads but break every later encode."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise ApiError(400, "invalid_unicode") from None


def _optional_str(payload, key, code):
    v = payload.get(key)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ApiError(400, code)
    _check_unicode(v)
    return v


def _valid_card_name(card):
    stem = card[:-3] if card.endswith(".md") else ""
    return (stem.strip() != ""
            and not any(x in card for x in ("/", "\\", ".."))
            and not card.startswith(".")
            and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in card)
            and len(card.encode("utf-8")) <= MAX_CARD_BYTES)


def read_and_validate(content_length_header, rfile, today):
    """Read the body and check every field before GitHub or Claude is called.
    Returns a ValidRequest; raises ApiError(400, code) on the first problem."""
    if content_length_header is None:
        raise ApiError(400, "invalid_content_length")
    v = str(content_length_header).strip(" \t")
    if not re.fullmatch(r"[0-9]+", v, re.ASCII):
        raise ApiError(400, "invalid_content_length")
    try:
        length = int(v)
    except ValueError:
        raise ApiError(400, "invalid_content_length") from None
    if length == 0:
        raise ApiError(400, "empty_body")
    if length > MAX_BODY_BYTES:
        raise ApiError(400, "body_too_large")

    try:
        payload = json.loads(rfile.read(length).decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError・JSONDecodeError は ValueError
        raise ApiError(400, "invalid_json") from None
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_payload")

    card = payload.get("card")
    if not isinstance(card, str):
        raise ApiError(400, "invalid_card")
    _check_unicode(card)
    if not _valid_card_name(card):
        raise ApiError(400, "invalid_card")

    qtype = payload.get("qtype")
    if not isinstance(qtype, str) or qtype not in QTYPE_LABEL:
        raise ApiError(400, "invalid_qtype")

    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise ApiError(400, "invalid_answers")
    answer_text, fill_answers = "", None
    if qtype == "fill":
        fill_answers = {}
        for field in ("claim", "why", "scene"):
            s = _optional_str(answers, field, "invalid_answers")
            if len(s) > MAX_FILL_CHARS:
                raise ApiError(400, "answer_too_long")
            fill_answers[field] = normalize_fill_answer(field, s)
    else:
        s = _optional_str(answers, "text", "invalid_answers")
        if len(s) > MAX_ANSWER_CHARS:
            raise ApiError(400, "answer_too_long")
        answer_text = s.strip()

    question = _optional_str(payload, "question", "invalid_question")
    if len(question) > MAX_QUESTION_CHARS:
        raise ApiError(400, "invalid_question")

    date = payload.get("date")
    if date is None:
        date = today.isoformat()
    else:
        if not isinstance(date, str):
            raise ApiError(400, "invalid_date")
        _check_unicode(date)
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date, re.ASCII):
            raise ApiError(400, "invalid_date")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ApiError(400, "invalid_date") from None

    test = payload.get("test")
    if test is not None and not isinstance(test, bool):
        raise ApiError(400, "invalid_test")

    return ValidRequest(card, qtype, answer_text, fill_answers, question, date)


def check_blank_answers(req):
    """Blank answers (and fill answers that are just a template placeholder) are answered
    with 200 and the existing error codes, without calling GitHub or Claude."""
    if req.qtype == "fill":
        if any(not v or is_placeholder_answer(f, v) for f, v in req.fill_answers.items()):
            return {"ok": False, "error": "incomplete_answer"}
    elif not req.answer_text:
        return {"ok": False, "error": "empty_answer"}
    return None


def origin_error(origin):
    """送信元の検査（2026-10-09 の方式 (b)）。ページ（ALLOWED_ORIGIN）以外からの呼び出しを止める。
    curl などは Origin を偽れるので、防げるのはブラウザで他のサイトから使われることまで。"""
    if origin != ALLOWED_ORIGIN:
        return ApiError(403, "forbidden_origin")
    return None


def reserve_daily_slot(today):
    """今日の採点の件数を1つ増やす（上限は DAILY_LIMIT）。件数は api_usage.json に、今日の分だけ書く。
    上限に達していたら daily_limit（429）。同時に2件が来て片方が負けたら busy（503）。"""
    key = today.isoformat()
    try:
        data = gh_request("GET", f"contents/{USAGE_FILE}?ref=main")
        counts = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
        sha = data["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        counts, sha = {}, None
    n = int(counts.get(key, 0)) if isinstance(counts, dict) else 0
    if n >= DAILY_LIMIT:
        raise ApiError(429, "daily_limit", {"message": DAILY_LIMIT_MESSAGE})
    body = {
        "message": f"api usage: {key}",
        "content": base64.b64encode(json.dumps({key: n + 1}).encode("utf-8")).decode("ascii"),
        "branch": "main",
        "committer": {"name": "recall-bot", "email": "actions@users.noreply.github.com"},
    }
    if sha is not None:
        body["sha"] = sha
    try:
        gh_request("PUT", f"contents/{USAGE_FILE}", body)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            raise ApiError(503, "busy", {"message": "少し待ってからもう一度送ってください"}) from None
        raise


def handle_request(content_length_header, rfile):
    """The whole POST /api/grade: returns (HTTP status, response dict)."""
    try:
        today = today_jst()  # 要求の最初に1回だけ決め、反映・再試行・date の既定値で使う
        req = read_and_validate(content_length_header, rfile, today)
        blank = check_blank_answers(req)
        if blank:
            return 200, blank
        reserve_daily_slot(today)  # 上限は Claude を呼ぶ前に確かめる
        card = load_card(req.card)
        if req.qtype == "fill":
            def apply_fn(c):
                return apply_fill(c["fm"], c["body"], req.fill_answers, today)
        else:
            result = fetch_grade(card, req.qtype, req.answer_text)  # Claude はこの1回だけ

            def apply_fn(c):
                return apply_graded(c["fm"], c["body"], req.qtype, req.answer_text,
                                    req.question, result, today)
        return 200, commit_with_retry(req.card, card, apply_fn, req.date)
    except ApiError as e:
        return e.status, {"ok": False, "error": e.code, **e.extra}
    except Exception as e:  # noqa: BLE001
        return 500, {"ok": False, "error": str(e)}


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status, obj):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        err = origin_error(self.headers.get("Origin"))
        if err:
            self._send_json(err.status, {"ok": False, "error": err.code})
            return
        try:
            status, obj = handle_request(self.headers.get("Content-Length"), self.rfile)
        except Exception as e:  # noqa: BLE001
            status, obj = 500, {"ok": False, "error": str(e)}
        self._send_json(status, obj)
