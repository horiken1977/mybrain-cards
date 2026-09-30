"""Vercel serverless function: grade a recall answer and update the card
file in the GitHub repo via the Contents API. Secrets (ANTHROPIC_API_KEY,
GITHUB_PAT) live only in Vercel environment variables, never in any
GitHub-tracked file, so GitHub's secret scanning never sees them."""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
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


def append_history(body, date, qtype_label, score, verdict, note):
    marker = "## 回答履歴\n"
    idx = body.rfind(marker)
    line = f"\n- {date} | {qtype_label} | {score} | {verdict} | {note}"
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


def next_review_after_pass(new_status, streak):
    days = INTERVAL_DAYS.get(new_status, 1)
    if new_status == "安定" and streak >= 1:
        days = 60 if streak > 1 else 30
    return (today_jst() + timedelta(days=days)).isoformat()


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


def load_card_from_github(filename):
    data = gh_request("GET", f"contents/{urllib.parse.quote(filename)}?ref=main")
    content = base64.b64decode(data["content"]).decode("utf-8")
    parsed = parse_card(content)
    parsed["sha"] = data["sha"]
    return parsed


def save_card_to_github(filename, new_text, sha, date):
    body = {
        "message": f"recall(web): update card state for {date}",
        "content": base64.b64encode(new_text.encode("utf-8")).decode("ascii"),
        "sha": sha,
        "branch": "main",
        "committer": {"name": "recall-bot", "email": "actions@users.noreply.github.com"},
    }
    gh_request("PUT", f"contents/{urllib.parse.quote(filename)}", body)


def grade_with_claude(title, claim_text, evidence_text, qtype_label, answer):
    prompt = f"""あなたは学習コーチです。以下のカードの主張と、ユーザーの回答を4軸で採点してください。

カード「{title}」
主張: {claim_text}
根拠: {evidence_text}
問いの型: {qtype_label}
ユーザーの回答: {answer}

4軸を各0〜2点で採点し、必ず次のJSON形式のみで返してください（説明文やマークダウンは付けない）：
{{"accuracy": 0-2, "example": 0-2, "conditions": 0-2, "action": 0-2, "feedback": "1〜2文の短いフィードバック（日本語）"}}
"""
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": MODEL, "max_tokens": 300,
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


def handle_fill(fm, body, answers):
    new_claim = (answers.get("claim") or "").strip()
    why = (answers.get("why") or "").strip()
    scene = (answers.get("scene") or "").strip()
    if not new_claim or not why or not scene:
        return None, {"ok": False, "error": "incomplete_answer"}

    body = re.sub(r"（AIの下書き。初回の /recall で自分の言葉に直す）\n?", "", body)
    body = re.sub(r"## 主張\n\n.+?\n\n## 根拠", f"## 主張\n\n{new_claim}\n\n## 根拠", body, flags=re.DOTALL)
    body = re.sub(r"（未記入：初回の /recall で自分の言葉で1行書く）", why, body)
    body = re.sub(r"- (.+?)（具体的な状況は初回の /recall で追記）",
                  lambda m: f"- {m.group(1)}：{scene}", body)

    today = today_jst()
    next_review = (today + timedelta(days=1)).isoformat()
    body = append_history(body, today.isoformat(), "補完", "-", "-", "初回記入(web)")
    new_text = write_frontmatter(fm, body, {
        "status": "要復習", "last_reviewed": today.isoformat(), "next_review": next_review,
    })
    return new_text, {"ok": True, "type": "fill"}


def handle_graded(fm, body, qtype, answers):
    answer = (answers.get("text") or "").strip()
    if not answer:
        return None, {"ok": False, "error": "empty_answer"}

    title = fm.get("title", "")
    result = grade_with_claude(title, get_claim(body), get_evidence(body), QTYPE_LABEL[qtype], answer)
    score = result["accuracy"] + result["example"] + result["conditions"] + result["action"]
    passed = score >= 5 and result["accuracy"] >= 1

    today = today_jst()
    status = fm.get("status", "未履修")
    streak = int(fm.get("streak", "0") or 0)

    hist = re.findall(r"- (\S+) \| (\S+) \| .+", body)
    same_as_last = bool(hist) and hist[-1][0] == today.isoformat() and hist[-1][1] == QTYPE_LABEL[qtype]

    if passed:
        if same_as_last:
            new_status, new_streak, next_review = status, streak, fm.get("next_review")
        else:
            new_streak = streak + 1
            new_status = advance_status(status)
            next_review = next_review_after_pass(new_status, new_streak)
        verdict = "合格"
    else:
        new_status = regress_status(status) if status not in ("未履修", "要復習") else status
        new_streak = max(0, streak - 1)
        next_review = (today + timedelta(days=1)).isoformat()
        verdict = "不合格"

    body = append_history(body, today.isoformat(), QTYPE_LABEL[qtype], f"{score}/8", verdict,
                           result.get("feedback", ""))
    new_text = write_frontmatter(fm, body, {
        "status": new_status, "last_reviewed": today.isoformat(),
        "next_review": next_review, "streak": str(new_streak),
    })
    response = {
        "ok": True, "type": "graded",
        "score": {"accuracy": result["accuracy"], "example": result["example"],
                  "conditions": result["conditions"], "action": result["action"], "total": score},
        "passed": passed, "feedback": result.get("feedback", ""),
        "new_status": new_status, "next_review": next_review,
    }
    return new_text, response


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            card_filename = payload["card"]
            qtype = payload["qtype"]
            answers = payload.get("answers", {})
            date = payload.get("date", today_jst().isoformat())

            card = load_card_from_github(card_filename)
            if card is None:
                result = {"ok": False, "error": "card_not_found"}
                new_text = None
            elif qtype == "fill":
                new_text, result = handle_fill(card["fm"], card["body"], answers)
            else:
                new_text, result = handle_graded(card["fm"], card["body"], qtype, answers)

            if new_text is not None:
                save_card_to_github(card_filename, new_text, card["sha"], date)

            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            self.send_response(500)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"))
