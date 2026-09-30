"""Pick today's cards and post them as a GitHub Issue (runs in GitHub Actions)."""
import json
import subprocess
from lib import (
    load_all_cards, select_cards, qtype_for, book_name, title, claim,
    evidence, other_card_for_contrast, QTYPE_LABEL, today_jst,
)

QUESTION_TEXT = {
    "recall": "本を見ずに、この考えを**自分の言葉で説明**してください。",
    "apply": "**今週の実際の場面を1つ挙げ**、その場面でこの考えをどう使うか説明してください。",
    "contrast": "この考えと、下の別の考え方が**どこで対立・補完するか**を説明してください。",
    "refute": "この考えが**成り立たない場面**を1つ挙げて説明してください。",
}


def build_question(card, qtype, all_cards):
    t = title(card)
    b = book_name(card)
    meta = {"card": card["path"].split("/")[-1], "qtype": qtype}
    lines = [f"<!-- recall-meta: {json.dumps(meta, ensure_ascii=False)} -->"]
    if qtype == "fill":
        lines.append(f"### 「{t}」（{b}） — まだ内容が確定していないカードです")
        lines.append("")
        lines.append(f"> {claim(card)}")
        lines.append("")
        lines.append("次の3つを書いてください（採点はありません。今日はこの記入だけでOK）：")
        lines.append("")
        lines.append("a) この考えを自分の言葉で1文")
        lines.append("b) なぜ自分に重要か（1行）")
        lines.append("c) 使う場面の具体的な状況（1つ）")
    else:
        lines.append(f"### 「{t}」（{b}） — {QTYPE_LABEL[qtype]}の問い")
        lines.append("")
        lines.append(QUESTION_TEXT[qtype])
        if qtype == "contrast":
            other = other_card_for_contrast(card, all_cards)
            if other:
                lines.append("")
                lines.append(f"比較対象：「{title(other)}」（{book_name(other)}） — {claim(other)}")
            else:
                lines.append("")
                lines.append("（比較対象がまだないため、代わりに：この考えが役立たない場面も1つ挙げてください）")
    return "\n".join(lines)


def main():
    cards = load_all_cards(".")
    if not cards:
        print("no cards found")
        return
    picked, mode = select_cards(cards, limit=3)
    if not picked:
        print("nothing due today")
        return

    date = today_jst().isoformat()
    body_parts = [
        f"今日（{date}）の想起テストです。回答は **このIssueへの1つのコメント** に、"
        "`A1.` `A2.` `A3.` のように問いの番号を付けて書いてください（1問だけならA1のみでOK）。",
        "",
        "---",
        "",
    ]
    for i, card in enumerate(picked, 1):
        qtype = "fill" if mode == "fill" else qtype_for(card)
        q = build_question(card, qtype, cards)
        body_parts.append(f"## Q{i}\n\n{q}\n")
    body = "\n".join(body_parts)

    title_line = f"想起テスト {date}"
    proc = subprocess.run(
        ["gh", "issue", "create", "--title", title_line, "--body", body,
         "--label", "recall"],
        capture_output=True, text=True,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr)
        raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
