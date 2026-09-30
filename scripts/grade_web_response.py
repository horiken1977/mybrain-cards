"""Grade an answer submitted from the web page (triggered via
repository_dispatch) and write the result back into the card file and
docs/today.json (runs in GitHub Actions)."""
import html
import json
import os
import re
from datetime import timedelta

import requests

from lib import (
    load_card, write_frontmatter, append_history, advance_status,
    regress_status, next_review_after_pass, today_jst, QTYPE_LABEL,
    claim as get_claim, evidence as get_evidence,
)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PAYLOAD = json.loads(os.environ["PAYLOAD"])
MODEL = "claude-haiku-4-5-20251001"

TODAY_JSON = "docs/today.json"


def grade_with_claude(card_title, claim_text, evidence_text, qtype_label, answer):
    prompt = f"""あなたは学習コーチです。以下のカードの主張と、ユーザーの回答を4軸で採点してください。

カード「{card_title}」
主張: {claim_text}
根拠: {evidence_text}
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
        json={"model": MODEL, "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0) if m else text)


def handle_fill(card, answers):
    new_claim = (answers.get("claim") or "").strip()
    why = (answers.get("why") or "").strip()
    scene = (answers.get("scene") or "").strip()
    if not new_claim or not why or not scene:
        return {"ok": False, "error": "incomplete_answer"}

    body = card["body"]
    body = re.sub(r"（AIの下書き。初回の /recall で自分の言葉に直す）\n?", "", body)
    body = re.sub(r"## 主張\n\n.+?\n\n## 根拠", f"## 主張\n\n{new_claim}\n\n## 根拠", body, flags=re.DOTALL)
    body = re.sub(r"（未記入：初回の /recall で自分の言葉で1行書く）", why, body)
    body = re.sub(r"- (.+?)（具体的な状況は初回の /recall で追記）",
                  lambda m: f"- {m.group(1)}：{scene}", body)

    today = today_jst()
    next_review = (today + timedelta(days=1)).isoformat()
    body = append_history(body, today.isoformat(), "補完", "-", "-", "初回記入(web)")

    card["body"] = body
    new_text = write_frontmatter(card, {
        "status": "要復習", "last_reviewed": today.isoformat(), "next_review": next_review,
    })
    with open(card["path"], "w", encoding="utf-8") as f:
        f.write(new_text)

    return {"ok": True, "type": "fill"}


def handle_graded(card, qtype, answers):
    answer = (answers.get("text") or "").strip()
    if not answer:
        return {"ok": False, "error": "empty_answer"}

    result = grade_with_claude(card["fm"]["title"], get_claim(card), get_evidence(card),
                                QTYPE_LABEL[qtype], answer)
    score = result["accuracy"] + result["example"] + result["conditions"] + result["action"]
    passed = score >= 5 and result["accuracy"] >= 1

    body = card["body"]
    today = today_jst()
    status = card["fm"].get("status", "未履修")
    streak = int(card["fm"].get("streak", "0") or 0)

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

    return {
        "ok": True, "type": "graded",
        "score": {"accuracy": result["accuracy"], "example": result["example"],
                  "conditions": result["conditions"], "action": result["action"], "total": score},
        "passed": passed, "feedback": html.escape(result.get("feedback", "")),
        "new_status": new_status, "next_review": next_review,
    }


def update_today_json(card_filename, result):
    if not os.path.exists(TODAY_JSON):
        return
    with open(TODAY_JSON, encoding="utf-8") as f:
        data = json.load(f)
    for q in data.get("questions", []):
        if q.get("card") == card_filename:
            q["result"] = result
    with open(TODAY_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    card_filename = PAYLOAD["card"]
    qtype = PAYLOAD["qtype"]
    answers = PAYLOAD.get("answers", {})

    card = load_card(card_filename)
    if card is None:
        result = {"ok": False, "error": "card_not_found"}
    elif qtype == "fill":
        result = handle_fill(card, answers)
    else:
        result = handle_graded(card, qtype, answers)

    update_today_json(card_filename, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
