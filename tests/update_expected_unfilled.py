"""Regenerate tests/expected_unfilled.json: the "unfilled" result of every card, as judged by
scripts/lib.py at a git revision against the cards at that same revision (file names and
true/false only; no card text goes into the file, since the repository is public).

    cd mybrain/cards && python3 tests/update_expected_unfilled.py            # HEAD
    cd mybrain/cards && python3 tests/update_expected_unfilled.py --rev <commit>

The first version was made with --rev 04713de (the parent of bc939b9, before the unfilled
check was rewritten), so the test compares the new check with an independent old result.
Re-run it when cards have been filled or added and you want the newer state pinned; read the
diff of the json before committing it (only the cards you filled should flip to false)."""
import argparse
import json
import os
import subprocess
import types

CARDS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(CARDS_DIR, "tests", "expected_unfilled.json")


def git(*args):
    return subprocess.run(["git", "-C", CARDS_DIR, *args], capture_output=True, check=True).stdout


def lib_at(rev):
    mod = types.ModuleType("lib_at_rev")
    exec(compile(git("show", f"{rev}:scripts/lib.py").decode("utf-8"), f"{rev}:scripts/lib.py", "exec"),
         mod.__dict__)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", default="HEAD", help="git revision for both scripts/lib.py and the cards")
    rev = git("rev-parse", "--short", ap.parse_args().rev).decode().strip()
    lib = lib_at(rev)
    names = [n for n in git("ls-tree", "-z", "--name-only", rev).decode("utf-8").split("\0") if n.endswith(".md")]
    cards = {}
    for name in sorted(names):
        text = git("show", f"{rev}:{name}").decode("utf-8")
        m = lib.FM_RE.match(text)
        if not m:
            continue
        fm = {k.strip(): lib.parse_scalar(v) for k, _, v in
              (line.partition(":") for line in m.group(1).split("\n") if ":" in line)}
        if fm.get("type") != "card":
            continue
        cards[name] = bool(lib.is_unfilled({"path": name, "fm": fm, "body": m.group(2), "raw": text}))
    data = {
        "_about": ("各カードが未記入か（true）記入済みか（false）の期待値。tests/test_lib_unfilled.py の A9b が "
                   "今の判定と比べる。ファイル名と真偽値だけ（本文は入れない）。カードを補完・追加したら古くなるが、"
                   "テストは「期待値にないカードは対象外」「未記入→記入済みは補完した印があれば可」とするので落ちない。"
                   "新しい状態で固定し直すときは更新コマンドを実行し、json の差分を確かめてから commit する"),
        "_update": "cd mybrain/cards && python3 tests/update_expected_unfilled.py [--rev <commit>]",
        "generated_from": rev,
        "unfilled": sum(cards.values()),
        "filled": len(cards) - sum(cards.values()),
        "cards": cards,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"{OUT}: {len(cards)} cards from {rev} (unfilled {data['unfilled']}, filled {data['filled']})")


if __name__ == "__main__":
    main()
