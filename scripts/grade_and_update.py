"""Parse the day's answer comment, grade it with Claude, update the card
files, and reply on the issue (runs in GitHub Actions)."""
import json
import os
import re
import subprocess

import requests

from lib import (
    load_card, write_frontmatter, append_history, advance_status,
    regress_status, next_review_after_pass, today_jst, QTYPE_LABEL,
)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ISSUE_NUMBER = os.environ["ISSUE_NUMBER"]
COMMENT_BODY = os.environ["COMMENT_BODY"]
MODEL = "claude-haiku-4-5-20251001"

META_RE = re.compile(r"<!-- recall-meta: (\{.*?\}) -->")
QBLOCK_RE = re.compile(r"^## Q(\d+)\n(.*?)(?=^## Q\d+|\Z)", re.DOTALL | re.MULTILINE)
ANSWER_RE = re.compile(r"^\s*A(\d+)[\.\)：:]\s*(.*?)(?=^\s*A\d+[\.\)：:]|\Z)",
                       re.DOTALL | re.MULTILINE | re.IGNORECASE)
FILL_PART_RE = re.compile(r"(?:^|\n)\s*[a-cA-C][\.\)]\s*(.+?)(?=\n\s*[a-cA-C][\.\)]|\Z)", re.DOTALL)


def get_issue_body():
    out = subprocess.run(
        ["gh", "issue", "view", ISSUE_NUMBER, "--json", "body", "-q", ".body"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def parse_questions(issue_body):
    qs = {}
    for m in QBLOCK_RE.finditer(issue_body):
        qnum, block = m.group(1), m.group(2)
        meta_m = META_RE.search(block)
        if not meta_m:
            continue
        meta = json.loads(meta_m.group(1))
        qs[qnum] = meta
    return qs


def parse_answers(comment_body, num_questions):
    answers = {}
    for m in ANSWER_RE.finditer(comment_body):
        answers[m.group(1)] = m.group(2).strip()
    if not answers and comment_body.strip() and num_questions == 1:
        # single-question shorthand: no "A1." prefix used (only safe when
        # the issue has exactly one question, otherwise we can't tell which
        # question the comment is answering)
        answers["1"] = comment_body.strip()
    return answers


def grade_with_claude(card_title, claim, evidence, qtype_label, answer):
    prompt = f"""あなたは学習コーチです。以下のカードの主張と、ユーザーの回答を4軸で採点してください。

カード「{card_title}」
主張: {claim}
根拠: {evidence}
問いの型: {qtype_label}
ユーザーの回答: {answer}

4軸を各0〜2点で採点し、必ず次のJSON形式のみで返してください（説明文やマークダウンは付けない）：
{{"accuracy": 0-2, "example": 0-2, "conditions": 0-2, "action": 0-2, "feedback": "1〜2文の短いフィードバック（日本語）"}}
"""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0) if m else text)


def handle_fill(card, answer):
    parts = [p.strip() for p in FILL_PART_RE.findall(answer)]
    if len(parts) < 3 or any(len(p) < 2 for p in parts):
        return (f"「{card['fm']['title']}」の回答をa)/b)/c)の3点に分けられませんでした。"
                "カードは更新していません。お手数ですが、次の形式で書き直して再送してください：\n"
                "a) 自分の言葉で1文\nb) なぜ自分に重要か\nc) 使う場面の具体的な状況")
    new_claim, why, scene = parts[0], parts[1], parts[2]

    body = card["body"]
    body = re.sub(r"（AIの下書き。初回の /recall で自分の言葉に直す）\n?", "", body)
    body = re.sub(r"## 主張\n\n.+?\n\n## 根拠", f"## 主張\n\n{new_claim}\n\n## 根拠", body, flags=re.DOTALL)
    body = re.sub(r"（未記入：初回の /recall で自分の言葉で1行書く）", why or "（未記入）", body)
    body = re.sub(r"- (.+?)（具体的な状況は初回の /recall で追記）",
                  lambda m: f"- {m.group(1)}：{scene}" if scene else m.group(0), body)

    tomorrow = today_jst()
    from datetime import timedelta
    next_review = (tomorrow + timedelta(days=1)).isoformat()
    body = append_history(body, tomorrow.isoformat(), "補完", "-", "-", "初回記入")

    card["body"] = body
    new_text = write_frontmatter(card, {
        "status": "要復習", "last_reviewed": tomorrow.isoformat(), "next_review": next_review,
    })
    with open(card["path"], "w", encoding="utf-8") as f:
        f.write(new_text)
    return f"「{card['fm']['title']}」記入ありがとうございます。明日から出題します。"


def handle_graded(card, qtype, answer):
    from lib import claim as get_claim, evidence as get_evidence
    result = grade_with_claude(card["fm"]["title"], get_claim(card), get_evidence(card),
                                QTYPE_LABEL[qtype], answer)
    score = result["accuracy"] + result["example"] + result["conditions"] + result["action"]
    passed = score >= 5 and result["accuracy"] >= 1

    body = card["body"]
    today = today_jst()
    status = card["fm"].get("status", "未履修")
    streak = int(card["fm"].get("streak", "0") or 0)

    # find last history entry's date+qtype to check "different day & different type"
    hist = re.findall(r"- (\S+) \| (\S+) \| .+", body)
    same_as_last = bool(hist) and hist[-1][0] == today.isoformat() and hist[-1][1] == QTYPE_LABEL[qtype]

    if passed:
        if same_as_last:
            new_status, new_streak, next_review = status, streak, card["fm"].get("next_review")
        else:
            new_streak = streak + 1
            new_status = advance_status(status)
            next_review = next_review_after_pass(new_status, new_streak)
        verdict = "合格"
    else:
        new_status = regress_status(status) if status not in ("未履修", "要復習") else status
        new_streak = max(0, streak - 1)
        from datetime import timedelta
        next_review = (today + timedelta(days=1)).isoformat()
        verdict = "不合格"

    body = append_history(body, today.isoformat(), QTYPE_LABEL[qtype], f"{score}/8", verdict,
                           result.get("feedback", ""))
    card["body"] = body
    new_text = write_frontmatter(card, {
        "status": new_status, "last_reviewed": today.isoformat(),
        "next_review": next_review, "streak": str(new_streak),
    })
    with open(card["path"], "w", encoding="utf-8") as f:
        f.write(new_text)

    return (f"「{card['fm']['title']}」{score}/8点・{verdict}（{new_status}）\n"
            f"{result.get('feedback', '')}")


def main():
    issue_body = get_issue_body()
    questions = parse_questions(issue_body)
    answers = parse_answers(COMMENT_BODY, num_questions=len(questions))

    replies = []
    for qnum, meta in questions.items():
        answer = answers.get(qnum)
        if not answer:
            continue
        card = load_card(meta["card"])
        if card is None:
            continue
        if meta["qtype"] == "fill":
            replies.append(handle_fill(card, answer))
        else:
            replies.append(handle_graded(card, meta["qtype"], answer))

    if not replies:
        print("no matching answers found; leaving issue open")
        if len(questions) > 1:
            subprocess.run(
                ["gh", "issue", "comment", ISSUE_NUMBER, "--body",
                 "回答を認識できませんでした。質問が複数あるIssueでは、`A1.` `A2.` `A3.` のように"
                 "番号を付けて回答してください（カードは変更していません）。"],
                check=False,
            )
        return

    comment = "\n\n---\n\n".join(replies)
    subprocess.run(["gh", "issue", "comment", ISSUE_NUMBER, "--body", comment], check=True)
    subprocess.run(["gh", "issue", "close", ISSUE_NUMBER], check=True)


if __name__ == "__main__":
    main()
