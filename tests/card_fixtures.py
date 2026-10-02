"""Fake cards for the tests (設計書 v4 §5.1 の雛形). Every text here is invented and short;
no real answer or personal information. The placeholder lines are written out literally
(not imported) so that a change in the code's constants makes the tests fail."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CARDS_DIR = os.path.dirname(HERE)
for sub in ("api", "scripts"):
    p = os.path.join(CARDS_DIR, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

CLAIM_MARK = "（AIの下書き。初回の /recall で自分の言葉に直す）"
WHY_MARK = "（未記入：初回の /recall で自分の言葉で1行書く）"
SCENE_MARK = "（未記入：使う場面を初回の補完で書く）"
OLD_SUFFIX = "（具体的な状況は初回の /recall で追記）"
OLD_LINES = {
    "- 部下・チーム（具体的な状況は初回の /recall で追記）",
    "- 顧客提案・商談（具体的な状況は初回の /recall で追記）",
    "- 自分自身（具体的な状況は初回の /recall で追記）",
}

EVIDENCE = "> 架空の引用文。\n\n[Kindle location 10](https://example.com/10)\n\n原本：`raw/kindle/架空の本.md`"
DRAFT_CLAIM = "架空の主張の下書き。\n\n" + CLAIM_MARK

# 回答に目印の文字列を含む補完（v2 の重大1の再現）
X_STAR = "下書きの「（AIの下書き。初回の /recall で自分の言葉に直す）」を消して書いた主張"
Y_STAR = "「（未記入」という表示を見たら自分で書く習慣をつけたい"
Z_STAR = "「（未記入：使う場面を初回の補完で書く）」を見たとき"
# 古い形の接尾辞で終わる具体的な回答（v3 の重大1の再現）
SCENE_DAGGER = "未記入の場面欄を見つけたときは、その場で自分の使用例を書く。画面の案内は（具体的な状況は初回の /recall で追記）"
SCENE_DDAGGER = "1on1 の記録欄を見たらその週の具体例を書く。画面の案内は（具体的な状況は初回の /recall で追記）"

FILL_HIST = "\n- 2026-10-01 | 補完 | - | - | 初回記入(web)"
FILL_HIST_TODAY = "\n- 2026-10-02 | 補完 | - | - | 初回記入(web)"
PASS_HIST = ("\n- 2026-10-02 | 想起 | 6/8 | 合格 | 架空のフィードバック"
             "\n  - 問い: 架空の問い"
             "\n  - 回答: 架空の回答")


def make_card(claim=DRAFT_CLAIM, why=WHY_MARK, scene="- " + SCENE_MARK, history="",
              evidence=EVIDENCE, status="未履修", streak="0", last_reviewed="",
              next_review="2026-10-02", book="架空の本"):
    return (
        "---\n"
        "title: 架空のカード\n"
        "type: card\n"
        f'book: "[[entities/{book}]]"\n'
        f"status: {status}\n"
        "priority: 高\n"
        f"last_reviewed: {last_reviewed}\n"
        f"next_review: {next_review}\n"
        f"streak: {streak}\n"
        "tags: [テスト]\n"
        "---\n"
        f"\n## 主張\n\n{claim}\n\n## 根拠\n\n{evidence}\n\n## なぜ自分に重要か\n\n{why}\n\n"
        f"## 使う場面\n\n{scene}\n\n## 回答履歴\n{history}"
    )


def body_of(text):
    """The body after the frontmatter (what parse_card / lib.load_card call body)."""
    return text.split("\n---\n", 1)[1]


def parse(text, path="架空のカード.md"):
    """The card dict that scripts/lib.load_card would return, without a file."""
    head, body = text[4:].split("\n---\n", 1)
    fm = {}
    for line in head.split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            fm[k.strip()] = v
    return {"path": path, "fm": fm, "body": body, "raw": text}


# (a) 採点用
A = make_card(claim="架空の主張。", why="架空の理由。", scene="- 架空の場面", status="学習中", streak="1",
              last_reviewed="2026-09-29", next_review="2026-10-02",
              history="\n- 2026-09-29 | 想起 | 6/8 | 合格 | 前回の一言")
# (b) 補完用（今のテンプレート）・(b2)(b3) 古い形
B = make_card()
B2 = make_card(scene="- 部下・チーム" + OLD_SUFFIX)
B3 = make_card(scene="- 自分自身" + OLD_SUFFIX)
# (c) 補完済み・(c*) 回答に目印の文字列・(c†)(c‡) 場面が古い形の接尾辞で終わる
# （(c†)(c‡) は変更前の handle_fill に (b)(b2) を当てた結果と1バイトも違わない形）
C = make_card(claim="X", why="Y", scene="- Z", history=FILL_HIST, status="要復習",
              last_reviewed="2026-10-01", next_review="2026-10-02")
C_STAR = make_card(claim=X_STAR, why=Y_STAR, scene="- " + Z_STAR, history=FILL_HIST, status="要復習",
                   last_reviewed="2026-10-01", next_review="2026-10-02",
                   evidence=EVIDENCE + "\n\n- 自分のメモ（location 1）：（未記入）のまま放置しない")
C_DAGGER = make_card(claim="X", why="Y", scene="- " + SCENE_DAGGER, history=FILL_HIST_TODAY, status="要復習",
                     last_reviewed="2026-10-02", next_review="2026-10-03")
C_DDAGGER = make_card(claim="X", why="Y", scene="- 部下・チーム：" + SCENE_DDAGGER, history=FILL_HIST_TODAY,
                      status="要復習", last_reviewed="2026-10-02", next_review="2026-10-03")
# (d) 補完のあと採点まで進んだもの
D = make_card(claim="X", why="Y", scene="- Z", history=PASS_HIST + FILL_HIST, status="学習中", streak="1",
              last_reviewed="2026-10-02", next_review="2026-10-05")
D_STAR = make_card(claim=X_STAR, why=Y_STAR, scene="- " + Z_STAR, status="学習中", streak="1",
                   last_reviewed="2026-10-02", next_review="2026-10-05",
                   evidence=EVIDENCE + "\n\n- 自分のメモ（location 1）：（未記入）のまま放置しない",
                   history=("\n- 2026-10-02 | 想起 | 6/8 | 合格 | 架空のフィードバック"
                            "\n  - 回答: 「（未記入：初回の /recall で自分の言葉で1行書く）」の欄に書いたことを実践した"
                            "\n  - 模範解答例: （AIの下書きを消して自分で書く、という架空の例）" + FILL_HIST))
D_DAGGER = make_card(claim="X", why="Y", scene="- " + SCENE_DAGGER, history=PASS_HIST + FILL_HIST,
                     status="学習中", streak="1", last_reviewed="2026-10-02", next_review="2026-10-05")
# (e) 一部記入済み（主張だけ書いて目印の行を消した）・(e2) 状態が進んでいる
E = make_card(claim="先の主張")
E2 = make_card(claim="先の主張", status="学習中", streak="1", last_reviewed="2026-10-01", next_review="2026-10-05")

ALL = {"a": A, "b": B, "b2": B2, "b3": B3, "c": C, "c*": C_STAR, "c†": C_DAGGER, "c‡": C_DDAGGER,
       "d": D, "d*": D_STAR, "d†": D_DAGGER, "e": E, "e2": E2}
EXPECTED_PENDING = {
    "a": set(), "b": {"claim", "why", "scene"}, "b2": {"claim", "why", "scene"}, "b3": {"claim", "why", "scene"},
    "c": set(), "c*": set(), "c†": set(), "c‡": set(), "d": set(), "d*": set(), "d†": set(),
    "e": {"why", "scene"}, "e2": {"why", "scene"},
}


def d2_bodies():
    """(c) をもとに1か所ずつ変えた本文。どれも未記入と数えない（§5.2 D2）。"""
    hist_marks = "".join(f"\n  - 回答: {m}" for m in (CLAIM_MARK, WHY_MARK, SCENE_MARK))
    out = {
        "1 根拠のメモに WHY_MARK だけの行": make_card(claim="X", why="Y", scene="- Z", evidence=EVIDENCE + "\n\n" + WHY_MARK),
        "2 回答履歴に目印の文": make_card(claim="X", why="Y", scene="- Z",
                                    history="\n- 2026-10-01 | 想起 | 6/8 | 合格 | 一言" + hist_marks
                                    + "\n" + CLAIM_MARK + "\n" + WHY_MARK + "\n- " + SCENE_MARK),
        "3 why の行が前置き＋目印": make_card(claim="X", why="前置き" + WHY_MARK, scene="- Z"),
        "4a why が（未記入）": make_card(claim="X", why="（未記入）", scene="- Z"),
        "4b why が（AIの下書き": make_card(claim="X", why="（AIの下書き", scene="- Z"),
        "5 why の節に CLAIM_MARK": make_card(claim="X", why=CLAIM_MARK, scene="- Z"),
        "6 主張の行が scene の目印": make_card(claim="- " + SCENE_MARK, why="Y", scene="- Z"),
        "7 scene の行が目印を含む": make_card(claim="X", why="Y", scene="- 「" + SCENE_MARK + "」を見たとき"),
        "8a 接尾辞で終わる具体的な回答": make_card(claim="X", why="Y", scene="- " + SCENE_DAGGER),
        "8b 3つにない分類": make_card(claim="X", why="Y", scene="- その他" + OLD_SUFFIX),
        "8c 分類の一部": make_card(claim="X", why="Y", scene="- 部下" + OLD_SUFFIX),
        "8d 接尾辞だけ": make_card(claim="X", why="Y", scene="- " + OLD_SUFFIX),
        "8e 記入済みの古い形": make_card(claim="X", why="Y",
                                    scene="- 部下・チーム：1on1 の記録欄に" + OLD_SUFFIX + "とあったら書く"),
        "8f 間に空白": make_card(claim="X", why="Y", scene="- 部下・チーム " + OLD_SUFFIX),
    }
    return out
