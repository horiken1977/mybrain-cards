"""Pick today's cards, publish a readable page for them on GitHub Pages,
and open a GitHub Issue to collect the answer (runs in GitHub Actions)."""
import html
import json
import os
import subprocess

LIMIT = int(os.environ.get("RECALL_LIMIT", "3"))
from lib import (
    load_all_cards, select_cards, qtype_for, book_name, title, claim,
    evidence, other_card_for_contrast, QTYPE_LABEL, today_jst,
)

PAGES_URL = "https://horiken1977.github.io/mybrain-cards/"

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


def q_html(i, card, qtype, all_cards):
    t = html.escape(title(card))
    b = html.escape(book_name(card))
    if qtype == "fill":
        head = f"「{t}」（{b}） — 内容確定前のカードです"
        body = f"""
          <blockquote>{html.escape(claim(card))}</blockquote>
          <p>次の3つを、回答用Issueに <code>A{i}.</code> として書いてください（採点はありません）：</p>
          <ol>
            <li>この考えを自分の言葉で1文</li>
            <li>なぜ自分に重要か（1行）</li>
            <li>使う場面の具体的な状況（1つ）</li>
          </ol>
        """
    else:
        head = f"「{t}」（{b}） — {QTYPE_LABEL[qtype]}の問い"
        extra = ""
        if qtype == "contrast":
            other = other_card_for_contrast(card, all_cards)
            if other:
                extra = (f"<p class='sub'>比較対象：「{html.escape(title(other))}」"
                         f"（{html.escape(book_name(other))}） — {html.escape(claim(other))}</p>")
            else:
                extra = "<p class='sub'>（比較対象がまだないため、代わりに：この考えが役立たない場面も1つ挙げてください）</p>"
        body = f"<p>{QUESTION_TEXT[qtype].replace('**', '')}</p>{extra}"
    return f"""
    <section class="q">
      <h2>Q{i}. {head}</h2>
      {body}
    </section>
    """


def build_page(date, questions_html, issue_url):
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>想起テスト {date}</title>
<style>
  body {{ font-family: -apple-system, "Hiragino Sans", sans-serif; max-width: 640px;
         margin: 0 auto; padding: 20px 16px 60px; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 1.3rem; margin-bottom: 4px; }}
  .date {{ color: #666; margin-bottom: 24px; }}
  section.q {{ background: #fff; border: 1px solid #e2e2e2; border-radius: 10px;
               padding: 16px 18px; margin-bottom: 16px; }}
  section.q h2 {{ font-size: 1.05rem; margin: 0 0 10px; }}
  blockquote {{ margin: 8px 0; padding: 8px 12px; background: #f4f4f4;
                border-left: 3px solid #999; font-size: 0.95rem; }}
  .sub {{ color: #555; font-size: 0.9rem; }}
  ol {{ padding-left: 1.2em; }}
  .answer-btn {{ display: block; text-align: center; background: #1a7f37; color: #fff;
                 text-decoration: none; padding: 14px; border-radius: 8px; font-weight: 600;
                 margin: 24px 0; }}
  .hint {{ color: #777; font-size: 0.85rem; text-align: center; }}
</style>
</head>
<body>
  <h1>想起テスト</h1>
  <div class="date">{date}</div>
  {questions_html}
  <a class="answer-btn" href="{issue_url}">この下のリンク先で回答する →</a>
  <p class="hint">回答はGitHub Issueのコメント欄に、A1. A2. A3. の形式で書いてください。</p>
</body>
</html>
"""


def main():
    cards = load_all_cards(".")
    if not cards:
        print("no cards found")
        return
    picked, mode = select_cards(cards, limit=LIMIT)
    if not picked:
        print("nothing due today")
        return

    date = today_jst().isoformat()

    # 1. Build the issue body (answer intake; keeps full question text + hidden
    #    metadata so the grading script can map A1/A2/A3 back to cards).
    body_parts = [
        f"読みやすいページはこちら → {PAGES_URL}",
        "",
        f"今日（{date}）の想起テストです。回答は **このIssueへの1つのコメント** に、"
        "`A1.` `A2.` `A3.` のように問いの番号を付けて書いてください（1問だけならA1のみでOK）。",
        "",
        "---",
        "",
    ]
    questions_html_parts = []
    for i, card in enumerate(picked, 1):
        qtype = "fill" if mode == "fill" else qtype_for(card)
        body_parts.append(f"## Q{i}\n\n{build_question(card, qtype, cards)}\n")
        questions_html_parts.append(q_html(i, card, qtype, cards))
    body = "\n".join(body_parts)

    title_line = f"想起テスト {date}"
    proc = subprocess.run(
        ["gh", "issue", "create", "--title", title_line, "--body", body, "--label", "recall"],
        capture_output=True, text=True,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr)
        raise SystemExit(proc.returncode)
    issue_url = proc.stdout.strip().splitlines()[-1]

    # 2. Publish the readable page pointing back at this issue for answering.
    page = build_page(date, "\n".join(questions_html_parts), issue_url)
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(page)
    print("wrote docs/index.html for", issue_url)


if __name__ == "__main__":
    main()
