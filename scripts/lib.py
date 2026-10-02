"""Shared helpers for the cards/ recall automation (GitHub Actions)."""
import re
import glob
import os
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

FM_RE = re.compile(r'^---\n(.*?)\n---\n(.*)$', re.DOTALL)

STATUS_ORDER = ["未履修", "要復習", "学習中", "要確認", "安定"]
INTERVAL_DAYS = {"要復習": 1, "学習中": 3, "要確認": 7, "安定": 30}
QTYPE_BY_STREAK = {0: "recall", 1: "apply", 2: "contrast", 3: "refute"}
QTYPE_LABEL = {"recall": "想起", "apply": "適用", "contrast": "対比・接続", "refute": "反証", "fill": "補完"}

# 補完の対象の節と、テンプレート（kindle_update.render_card・/card）が書く未記入の目印の行。
# api/grade.py と同じ判定（Vercel の関数は scripts/ を import しないので2か所に置き、tests でずれを防ぐ）
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


def load_card(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
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
    return {"path": path, "fm": fm, "body": body, "raw": text}


def load_all_cards(cards_dir="."):
    cards = []
    for path in sorted(glob.glob(os.path.join(cards_dir, "*.md"))):
        c = load_card(path)
        if c and c["fm"].get("type") == "card":
            cards.append(c)
    return cards


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


def is_unfilled(card):
    """未記入＝主張・なぜ自分に重要か・使う場面のどれかの節に、テンプレートの目印の行が残っている（採点APIと同じ判定）"""
    return bool(pending_sections(card["body"]))


def days_overdue(card):
    nr = card["fm"].get("next_review", "").strip()
    if not nr:
        return 0
    try:
        d = datetime.strptime(nr, "%Y-%m-%d").date()
    except ValueError:
        return 0
    return (today_jst() - d).days


def is_due(card):
    if card["fm"].get("status") == "未履修":
        return True
    return days_overdue(card) >= 0


PRIORITY_RANK = {"高": 0, "中": 1, "低": 2}
# 補完（未記入カードの記入）は1日この数まで。残りは採点ありの問いに回す（リポジトリ変数 RECALL_FILL_LIMIT で変更可）
MAX_FILL_PER_DAY = int(os.environ.get("RECALL_FILL_LIMIT", "1"))


def select_cards(cards, limit=3):
    """Pick up to `limit` (card, qtype) pairs for today, never two from the same
    book. At most MAX_FILL_PER_DAY unfilled cards (fill) come first, then due
    cards sorted by priority / overdue days. If there are not enough due cards,
    the remaining slots go to further fill questions. If too few books are
    available, fewer questions are returned rather than repeating a book."""
    def prio(c):
        return PRIORITY_RANK.get(c["fm"].get("priority"), 1)

    # 補完の候補は、自分のメモが付いたハイライトを根拠に持つカードを先に出す
    unfilled = sorted([c for c in cards if is_unfilled(c)],
                      key=lambda c: (prio(c), 0 if "自分のメモ" in c["body"] else 1))
    due = sorted([c for c in cards if not is_unfilled(c) and is_due(c)],
                 key=lambda c: (prio(c), -days_overdue(c)))
    candidates = [(c, "fill") for c in unfilled] + [(c, qtype_for(c)) for c in due]

    picked = []
    used_books = set()
    fills = 0
    for card, qtype in candidates:
        if len(picked) >= limit:
            break
        book = book_name(card)
        if book in used_books:
            continue
        if qtype == "fill":
            if fills >= MAX_FILL_PER_DAY:
                continue
            fills += 1
        picked.append((card, qtype))
        used_books.add(book)

    # 採点ありの問いが足りない日は、空いた枠を補完の問いで埋める
    for card in unfilled:
        if len(picked) >= limit:
            break
        book = book_name(card)
        if book in used_books or any(card is c for c, _ in picked):
            continue
        picked.append((card, "fill"))
        used_books.add(book)
    return picked


def qtype_for(card):
    streak = 0
    try:
        streak = int(card["fm"].get("streak", "0"))
    except ValueError:
        pass
    return QTYPE_BY_STREAK.get(streak, "refute")


def book_name(card):
    b = card["fm"].get("book", "")
    return b.replace('"', "").replace("[[entities/", "").replace("]]", "").strip()


def title(card):
    return card["fm"].get("title", os.path.splitext(os.path.basename(card["path"]))[0])


def claim(card):
    m = re.search(r"## 主張\n\n(.+?)\n\n", card["body"], re.DOTALL)
    return m.group(1).strip() if m else ""


def evidence(card):
    m = re.search(r"## 根拠\n\n(.+?)\n\n(?:原本|##)", card["body"], re.DOTALL)
    return m.group(1).strip() if m else ""


def other_card_for_contrast(card, all_cards):
    others = [c for c in all_cards if c["path"] != card["path"] and not is_unfilled(c)]
    return others[0] if others else None


def write_frontmatter(card, updates):
    fm = card["fm"]
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
    new_fm = "\n".join(lines)
    return new_fm + "\n" + card["body"]


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
