"""Bring cards/ up to date with the Kindle highlights sync.

Runs in the kindle-highlights (private) GitHub Actions right after the Notion sync,
with this repo checked out next to it. The raw highlights never come into this
public repo: only the card files (a ~100-character quote + the memo) are written.

  A. memo      : a card's evidence highlight got a memo (or the memo changed) -> update
                 the "- 自分のメモ（location N）：" line. Every card is checked every day.
  B. check     : the card's quote is no longer in the highlight (edited / deleted in
                 Notion) -> not rewritten, only reported.
  C. new cards : books with no card, and books with fewer than 3 cards that got new
                 highlights or memos today -> Claude picks ideas (same rules as the
                 /card bulk mode) and drafts the claim. The user writes the rest in
                 the daily fill question.
  D. swap      : a book that already has 3 cards got a new highlight with a memo ->
                 only reported.

Only the evidence memo lines and new card files are written. Claims, "why it matters",
"when to use", answer history and status are never touched; no card is deleted.

Usage: python scripts/kindle_update.py --kindle <kindle-highlights dir> [--dry-run] [--no-ai] [--backfill]
Reads  <kindle>/.sync/changes.json (written by .sync/sync.py)
Writes <kindle>/.sync/cards_report.json (mailed by `sync.py --send-email`)
Env:   ANTHROPIC_API_KEY (for C)
"""
import argparse
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402

CARDS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = "claude-sonnet-5-5"
MAX_CARDS_PER_BOOK = 3
MAX_QUOTE_CHARS = 120  # 「約100字まで」に少し余裕を持たせた上限
MEMO_RE = re.compile(r"^- 自分のメモ（location (\d+)）：(.*)$", re.M)
SRC_RE = re.compile(r"原本：`raw/kindle/(.+?)`")


def nfc(s):
    return unicodedata.normalize("NFC", s)


def squash(s):
    return re.sub(r"\s+", "", s)


def one_line(s):
    return " / ".join(l.strip() for l in s.splitlines() if l.strip())


# ------------------------------------------------------------------ loading
def load_raw(kindle_dir, parse_file):
    raw = {}
    for name in os.listdir(kindle_dir):
        if name.endswith(".md") and not name.startswith("."):
            _, hls = parse_file(os.path.join(kindle_dir, name))
            raw[nfc(name)] = hls
    return raw


def evidence_section(body):
    m = re.search(r"## 根拠\n(.*?)(?=\n## )", body, re.S)
    return m.group(1) if m else ""


def card_info(card):
    ev = evidence_section(card["body"])
    return {
        "card": card,
        "title": lib.title(card),
        "book": lib.book_name(card),
        "src": [nfc(s) for s in SRC_RE.findall(ev)],
        "locs": list(dict.fromkeys(re.findall(r"location=(\d+)", ev))),
        "quotes": [l[2:].strip() for l in ev.split("\n") if l.startswith("> ")],
        "memos": dict(MEMO_RE.findall(ev)),
    }


def quote_in(quote, text):
    """A card quote may skip words with … ; every piece must be in the highlight."""
    parts = [p for p in re.split(r"…|\.\.\.", quote) if squash(p)]
    return bool(parts) and all(squash(p) in squash(text) for p in parts)


def evidence_highlights(info, hls):
    """loc -> the raw highlight the card quotes at that location (None if not found)."""
    out = {}
    for loc in info["locs"]:
        at_loc = [h for h in hls if h["loc"] == loc]
        hit = next((h for h in at_loc if any(quote_in(q, h["text"]) for q in info["quotes"])), None)
        if hit is None and len(at_loc) == 1 and not info["quotes"]:
            hit = at_loc[0]
        out[loc] = hit
    return out


# ------------------------------------------------------------- A / B: follow
def follow_card(info, hls):
    """Returns (new card text or None, [memo updates], [problems])."""
    text, updates, problems = info["card"]["raw"], [], []
    ev_hls = evidence_highlights(info, hls)
    for loc, h in ev_hls.items():
        if h is None:
            if not any(x["loc"] == loc for x in hls):
                problems.append(f"Location {loc} のハイライトが Notion にない（削除された可能性）")
            else:
                problems.append(f"根拠の引用が Location {loc} のハイライトに見つからない（本文が変わった可能性）")
            continue
        memo = one_line(h["memo"])
        if not memo or squash(memo) == squash(info["memos"].get(loc, "")):
            continue
        line = f"- 自分のメモ（location {loc}）：{memo}"
        if loc in info["memos"]:
            text = re.sub(rf"^- 自分のメモ（location {loc}）：.*$", lambda _: line, text, count=1, flags=re.M)
        else:
            text = text.replace("\n原本：`", f"\n{line}\n\n原本：`", 1)
        updates.append(loc)
    return (text if updates else None), updates, problems


# ---------------------------------------------------------- C: new cards
SYSTEM = """あなたは読書の定着のための「アイデアカード」を作る係です。
1冊の本のハイライト（とその人のメモ）から、カードにするアイデアを選び、主張の下書きを書きます。
カードは本人が毎朝の想起テストで使います。主張は本人があとで自分の言葉に直すので、下書きで構いません。

規則：
- 作るのは最大 {slots} 枚。ハイライトが少ない・カードにする価値のあるアイデアが少ないときは、少なくてよい（0枚も可）
- 選ぶ優先順位：①本人のメモ付きのハイライト（メモは本人の関心の表れ） ②本の中心的な主張 ③他の本のカードと繋がる論点
- 既存カード（一覧を渡す）と同じ論点は避ける。同じ本のカードとも重ならないようにする
- title：アイデアの一言（日本語20字前後。ファイル名になるので / \\ : * ? " < > | を使わない）
- quote：根拠のハイライト本文から、要点だけを一字一句そのまま抜き出す（{max_quote}字以内。言い換え・要約・語の追加はしない。途中を省くときは … を使う）
- loc：その quote を抜き出したハイライトの Location（渡した値をそのまま）
- claim：主張の下書き1〜2文。本の主張を、本人が自分の仕事・生活で使える形で言い切る
- tags：1〜2個。既存のタグ一覧にあるものを優先する
- book：本の短い書名（副題・レーベル・括弧書きを除く。例「経営を見る眼」「スタンフォード式 最高の睡眠」）"""

SCHEMA = {
    "type": "object",
    "properties": {
        "book": {"type": "string"},
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "loc": {"type": "string"},
                    "quote": {"type": "string"},
                    "claim": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "loc", "quote", "claim", "tags"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["book", "cards"],
    "additionalProperties": False,
}


def ask_claude(client, title, candidates, slots, book_cards, all_titles, tag_vocab):
    hl_lines = []
    for h in candidates:
        memo = f"\n  本人のメモ：{one_line(h['memo'])}" if h["memo"] else ""
        hl_lines.append(f"- Location {h['loc']}：{h['text'] or '（本文なし）'}{memo}")
    same_book = "\n".join(f"- {t}" for t in book_cards) or "（なし）"
    user = (f"# 本\n{title}\n\n# 候補のハイライト\n" + "\n".join(hl_lines) +
            f"\n\n# この本の既存カード\n{same_book}\n\n# 他の本の既存カード（論点の重複を避ける）\n" +
            "、".join(all_titles) + "\n\n# 既存のタグ一覧\n" + "、".join(tag_vocab))
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM.format(slots=slots, max_quote=MAX_QUOTE_CHARS),
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined ({getattr(resp.stop_details, 'category', None)})")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off (max_tokens)")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def safe_title(t):
    return re.sub(r'[\\/:*?"<>|]', "・", nfc(t)).strip()


def render_card(title, book, tags, claim, quote, h, src, today):
    memo = f"- 自分のメモ（location {h['loc']}）：{one_line(h['memo'])}\n\n" if h["memo"] else ""
    return (
        "---\n"
        f"title: {title}\n"
        "type: card\n"
        f'book: "[[entities/{book}]]"\n'
        "status: 未履修\n"
        "priority: 高\n"  # 読んだ直後の本を、翌朝の補完の問いに先に出すため
        "last_reviewed:\n"
        f"next_review: {today}\n"
        "streak: 0\n"
        f"tags: [{', '.join(tags)}]\n"
        "---\n\n"
        f"## 主張\n\n{claim.strip()}\n\n（AIの下書き。初回の /recall で自分の言葉に直す）\n\n"
        f"## 根拠\n\n> {quote}\n\n[Kindle location {h['loc']}]({h['url']})\n\n{memo}"
        f"原本：`raw/kindle/{src}`\n\n"
        "## なぜ自分に重要か\n\n（未記入：初回の /recall で自分の言葉で1行書く）\n\n"
        "## 使う場面\n\n- （未記入：使う場面を初回の補完で書く）\n\n"
        "## 回答履歴\n"
    )


def build_new_cards(client, src, book_title, candidates, slots, book, book_cards, all_titles, tag_vocab, today):
    """Ask Claude, validate every card mechanically, and return [(filename, text, title, book)].
    `all_titles` (every card title, including the ones made earlier in this run) is extended."""
    ans = ask_claude(client, book_title, candidates, slots, book_cards, all_titles, tag_vocab)
    book = book or nfc(ans["book"]).strip() or book_title
    out, used_titles = [], {nfc(t) for t in all_titles}
    for c in ans["cards"][:slots]:
        title = safe_title(c["title"])
        quote = one_line(c["quote"]).strip("「」 ")
        loc = re.sub(r"\D", "", c["loc"])  # the model may answer "Location 104"
        h = next((x for x in candidates if x["loc"] == loc and quote_in(quote, x["text"])), None)
        if not title or title in used_titles or os.path.exists(os.path.join(CARDS_DIR, title + ".md")):
            print(f"  skip (title taken): {title}")
            continue
        if h is None or len(quote) > MAX_QUOTE_CHARS:
            print(f"  skip (quote not verbatim or too long): {title} / L{c['loc']} {quote[:40]}")
            continue
        tags = [t.strip() for t in c["tags"] if t.strip()][:2]
        out.append((title + ".md", render_card(title, book, tags, c["claim"], quote, h, src, today), title, book))
        used_titles.add(title)
        all_titles.append(title)
    return out


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kindle", required=True, help="kindle-highlights checkout (raw/kindle)")
    ap.add_argument("--dry-run", action="store_true", help="report only; write no card")
    ap.add_argument("--no-ai", action="store_true", help="skip C (new cards); A/B/D only")
    ap.add_argument("--backfill", action="store_true",
                    help="also make cards for every book that has none (normally only books changed today)")
    args = ap.parse_args()

    sys.path.insert(0, os.path.join(args.kindle, ".sync"))
    import sync  # the same parser that writes raw/kindle

    report_path = os.path.join(args.kindle, ".sync", "cards_report.json")
    report = {"new_cards": [], "memo_updated": [], "needs_check": [], "swap_candidates": [], "error": ""}
    changes_path = os.path.join(args.kindle, ".sync", "changes.json")
    changes = json.load(open(changes_path, encoding="utf-8"))["changes"] if os.path.exists(changes_path) else []
    changed = {nfc(c["file"]): c for c in changes if c.get("file")}
    today = lib.today_jst().isoformat()

    raw = load_raw(args.kindle, sync.parse_file)
    infos = [card_info(c) for c in lib.load_all_cards(CARDS_DIR)]
    writes = {}  # filename -> text

    # A / B: every card against its raw highlight
    needs_total = 0
    for info in infos:
        if not info["src"] or info["src"][0] not in raw:
            continue
        src = info["src"][0]
        text, memo_locs, problems = follow_card(info, raw[src])
        if text:
            writes[os.path.basename(info["card"]["path"])] = text
            report["memo_updated"] += [{"title": info["title"], "loc": loc} for loc in memo_locs]
        needs_total += len(problems)
        if src in changed:  # report only books that changed today, so the mail does not repeat daily
            report["needs_check"] += [{"title": info["title"], "reason": p} for p in problems]
    report["needs_check_total"] = needs_total

    # cards per book (a book's two editions share the same `book:` name)
    by_src, by_book = {}, {}
    for info in infos:
        by_book.setdefault(info["book"], []).append(info["title"])
        for s in info["src"]:
            by_src.setdefault(s, info["book"])
    used = {(s, loc) for i in infos for s in i["src"] for loc in i["locs"]}

    # C: which books get new cards, from which highlights
    targets = []
    for src, hls in sorted(raw.items()):
        book = by_src.get(src)
        have = len(by_book.get(book, [])) if book else 0
        if not hls or have >= MAX_CARDS_PER_BOOK:
            continue
        if book is None:  # a book with no card at all (Claude may judge it worth none: ask again only when it changes)
            if src in changed or args.backfill:
                targets.append((src, None, hls, MAX_CARDS_PER_BOOK))
        elif src in changed:
            c = changed[src]
            fresh = [h for h in c["new"] + c["memo_updates"] if (src, h["loc"]) not in used]
            if fresh:
                targets.append((src, book, fresh, MAX_CARDS_PER_BOOK - have))

    tag_vocab = sorted({t.strip() for i in infos for t in i["card"]["fm"].get("tags", "").strip("[]").split(",")
                        if t.strip()})
    if targets and not args.no_ai:
        import anthropic
        client = anthropic.Anthropic()
        errors, all_titles = [], [i["title"] for i in infos]
        for src, book, candidates, slots in targets:
            title = changed.get(src, {}).get("title") or os.path.splitext(src)[0]
            print(f"new cards: {title} (slots {slots}, candidates {len(candidates)})")
            try:
                made = build_new_cards(client, src, title, candidates, slots, book,
                                       by_book.get(book, []), all_titles, tag_vocab, today)
            except (anthropic.APIStatusError, anthropic.APIConnectionError, RuntimeError,
                    json.JSONDecodeError, KeyError) as e:
                errors.append(f"{title}: {e}")
                print(f"  error: {e}")
                continue
            for fname, text, ctitle, cbook in made:
                writes[fname] = text
                report["new_cards"].append({"title": ctitle, "book": cbook})
                by_book.setdefault(cbook, []).append(ctitle)
                by_src.setdefault(src, cbook)
        report["error"] = " ／ ".join(errors)
    elif targets:
        print("new cards skipped (--no-ai): " + ", ".join(t[0] for t in targets))

    # D: books already full that got memo-bearing highlights today
    for src, c in changed.items():
        book = by_src.get(src)
        if book and len(by_book.get(book, [])) >= MAX_CARDS_PER_BOOK:
            for h in c["new"] + c["memo_updates"]:
                if h["memo"] and (src, h["loc"]) not in used:
                    report["swap_candidates"].append({"book": book, "loc": h["loc"], "memo": one_line(h["memo"])})

    new_names = {c["title"] + ".md" for c in report["new_cards"]}
    for fname, text in writes.items():
        print(f"{'(dry-run) ' if args.dry_run else ''}write: {fname}")
        if args.dry_run and fname in new_names:  # show the drafts for review (private repo log)
            print("    " + text.split("\n## なぜ自分に重要か")[0].replace("\n", "\n    "))
        if not args.dry_run:
            with open(os.path.join(CARDS_DIR, fname), "w", encoding="utf-8") as f:
                f.write(text)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in report.items()}, ensure_ascii=False))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"\n### Cards\n- new cards: {len(report['new_cards'])}\n"
                    f"- memo updated: {len(report['memo_updated'])}\n"
                    f"- needs check: {len(report['needs_check'])} today / {needs_total} total\n"
                    f"- swap candidates: {len(report['swap_candidates'])}\n")
            for c in report["new_cards"]:
                f.write(f"  - {c['title']}（{c['book']}）\n")
            if report["error"]:
                f.write(f"- error: {report['error']}\n")


if __name__ == "__main__":
    main()
