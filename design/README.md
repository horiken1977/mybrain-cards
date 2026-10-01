# mybrain-cards 設計書

読書（Kindleハイライト）を「AIが探せる状態」で終わらせず「自分の頭から取り出せる状態」まで持っていくための、間隔反復＋想起テストの仕組み。mybrain本体（`raw/`・`wiki/`）とは別の独立リポジトリとして運用する。

設計の出発点は mybrain側の `wiki/analysis/kindle-reading-retention-design.md`（Qiita「気合ではなくログで管理する学習法」を参考にした最初の設計）。本書は**現在動いている実装（GitHub Pages＋Vercelサーバーレス関数でWebページ内に回答・採点・結果表示を完結させる方式）を正として**記述する。旧方式からの経緯は §12 にまとめる。

> 最終同期：2026-10-01（コミット `23948d9` 時点の実装に合わせて全面改訂。同日、MVP運用（1日1問）を反映）
>
> **現在のフェーズ：MVP（1日1問）でテスト運用中**。詳細は §12.4。

---

## 1. 目的

- ingest（mybrain側）は「AIが要約して検索できる状態」を作るだけで、人間の記憶には効かない
- カードは「AIを出題係にして、本人に思い出させる／自分の言葉で言わせる」ことで定着させる
- 毎日欠かさず回答できるよう、Mac起動に依存しないスマホ運用を主軸にする
- 回答・採点・結果確認は**すべて1つのWebページ内**で完結させ、別の場所（Issue等）への移動を発生させない

## 2. 全体アーキテクチャ

GitHub Pagesは静的サイトのためそれ単体ではフォーム送信を受け取れない。そこで**サーバーレス関数**を1つ挟み、静的なページ配信はGitHub Pages、回答受付・採点・データ更新は **Vercel の Python 関数（`api/grade.py`）** が担う。

```
┌──────────────────────────────────────────────────────────────┐
│ 毎朝08:00 JST（GitHub Actions: daily-question.yml）             │
│  1. 出題カードを選ぶ（lib.select_cards）                        │
│  2. docs/today.json・docs/index.html を生成                     │
│  3. 通知専用Issueを作成→即close（GitHubの通知メール/pushを飛ばす） │
│  4. git push（docs/）→ GitHub Pagesに反映                       │
└───────────────────────────┬──────────────────────────────────┘
                             │ 通知メール（本文はPagesリンクのみ）
                             ▼
                    スマホでメールを開き、Pagesのリンクをタップ
                             │
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ GitHub Pages（https://horiken1977.github.io/mybrain-cards/）   │
│  - today.json を読み込み、問いカードごとに回答欄を表示            │
│  - 送信ボタン押下 → fetch() で下記関数へ POST                   │
│  - 関数からのレスポンス（点数・フィードバック）をその場に表示      │
└───────────────────────────┬──────────────────────────────────┘
                             │ POST https://mybrain-cards.vercel.app/api/grade
                             │      { date, card, qtype, answers }
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ Vercel サーバーレス関数（api/grade.py, Python）                  │
│  1. GitHub Contents APIでカードファイルを取得                    │
│  2. fill→記入を反映／それ以外→Claude API（Haiku 4.5）で4軸採点    │
│  3. GitHub Contents APIでカードファイルを更新（commit）           │
│  4. 採点結果（点数・フィードバック・新status）をJSONで返す         │
└──────────────────────────────────────────────────────────────┘
                             │
                             ▼
              Mac側 `/card`・`/recall` は実行前後に
              git pull / push して同じcardsリポジトリと同期
```

## 3. リポジトリ構成

```
cards/                        # mybrain直下。独立git repo（public）
  <カードタイトル>.md           # 1アイデア1ファイル（type: card）
  dashboard.base               # Obsidian Basesダッシュボード（今日やる／弱点／状態別／本別）
  api/
    grade.py                   # Vercelサーバーレス関数：POST /api/grade（採点・カード更新）
  design/
    README.md                  # 設計書本体（このファイル）
    TODO.md                    # 未着手・対応中のタスク一覧
  scripts/
    lib.py                     # カードのfrontmatter読み書き・選定ロジック共通処理
    select_and_post.py         # 出題カード選定・docs/today.json・docs/index.html生成・通知Issue
  docs/
    .nojekyll                  # Jekyll処理を無効化（素の静的ファイルとして配信）
    index.html                 # 「今日の想起テスト」ページ（daily-question.ymlが毎日生成・上書き）
    today.json                 # その日の出題データ（同上）
  .github/workflows/
    daily-question.yml         # 毎日08:00 JST起動（cron `0 23 * * *` UTC）＋手動実行（limit指定可）。出題数は §5
  .vercel/                     # Vercel CLIのプロジェクトリンク（.gitignore対象・ローカルのみ）
```

- **GitHub repo**: `horiken1977/mybrain-cards`（**public**。GitHub PagesがFreeプランではprivate repoで使えないため2026-09-30にpublic化）
- **GitHub Pages**: `main` ブランチの `/docs` から配信（`https://horiken1977.github.io/mybrain-cards/`）
- **Vercel**: プロジェクト `horikens-projects/mybrain-cards`、本番URL `https://mybrain-cards.vercel.app`。`api/*.py` はVercelが自動でPython関数として検出するため **`vercel.json` は置かない**（`functions.runtime: "python3.12"` 指定は「Function Runtimes must have a valid version」でビルドが失敗したため削除）
- **デプロイ**：Mac の `cards/` で Vercel CLI（`vercel --prod`）を実行する。GitHub連携による自動デプロイは未確認のため、`api/grade.py` を変更したら手動デプロイが必要
- **Deployment Protection**：Vercel ダッシュボードの「Vercel Authentication（Require Log In）」を **OFF** にしている（2026-09-30）。ONのままだと、独自ドメインなしでは `mybrain-cards.vercel.app` にも保護がかかり、ブラウザ（Pages）から採点APIを呼べないため

## 4. データモデル（カード）

```yaml
---
title: カードタイトル（アイデアの一言）
type: card
book: "[[entities/書籍エンティティ名]]"   # mybrain側のentityへのwikilink文字列
status: 未履修          # 未履修 / 要復習 / 学習中 / 要確認 / 安定
priority: 中            # 高 / 中 / 低
last_reviewed:          # YYYY-MM-DD（未実施なら空）
next_review: YYYY-MM-DD
streak: 0               # 前回と別の日・別の型の問いに連続合格した回数
tags: []
---
```

本文の見出し：`## 主張`（自分の言葉で1文）／`## 根拠`（ハイライト原文＋Kindle locationリンク＋自分のメモ＋原本パス）／`## なぜ自分に重要か`（本人が1行）／`## 使う場面`（「〇〇のとき、これを使う」）／`## 回答履歴`（`- 日付 | 型 | 点数 | 合否 | 一言フィードバック`）。

新規カードが未記入（「なぜ自分に重要か」等に `（未記入：…）` が残っている）の間は、出題ロジックが最優先で「補完（fill）」の問いにする。

## 5. 出題ロジック（`lib.select_cards`）

1. **未記入カードがあれば最優先**（最大3枚、記入させるだけで採点なし）
2. 無ければ `next_review <= 今日` または `status: 未履修` の「期限到来カード」を対象に：
   - `priority`（高→中→低）
   - 期限超過日数が長い順
   - 異なる本が混ざるよう選ぶ（同じ本ばかりにしない）
3. 出題数の上限は次の順で決まる：手動実行時の `limit` 入力 → リポジトリ変数 `RECALL_LIMIT` → 既定値 **1**
   - MVP期間は1日1問（`RECALL_LIMIT` 未設定）。本運用で3問に増やすときは `gh variable set RECALL_LIMIT --body 3 -R horiken1977/mybrain-cards`（コード変更不要）

### 問いの型（`streak` で決まる）

| streak | 型 | 問い方 |
|---|---|---|
| 0 | 想起 | 本を見ずに自分の言葉で説明 |
| 1 | 適用 | 今週の実際の場面でどう使うか |
| 2 | 対比・接続 | 別カードの主張とどこで対立・補完するか |
| 3 | 反証 | 成り立たない場面は何か |
| 4以上 | ローテーション | 上記を回す |

### 採点（Vercel関数 → Claude API）

モデルは `claude-haiku-4-5-20251001`（`max_tokens: 300`）。4軸（主張の正確さ／自分の具体例／適用条件と限界／次の行動）を各0〜2点、合計8点満点。**合計5点以上かつ主張の正確さ1点以上で合格**。

### 状態更新

| 結果 | 更新 |
|---|---|
| 合格（前回と別の日・別の型） | `streak`+1、状態を1段階進める |
| 合格（同日・同型の再挑戦） | 状態・`streak`・`next_review` は変えない |
| 不合格 | 状態を1段階戻す（未履修・要復習のときは据え置き）、`streak`-1（下限0）、`next_review`は翌日 |
| 補完（fill）の記入 | 主張・なぜ重要か・使う場面を書き換え、`status: 要復習`、`next_review`は翌日 |

次回確認日（合格時）：要復習=1日 → 学習中=3日 → 要確認=7日 → 安定=30日（安定で合格継続なら60日）。

## 6. 画面設計

複数ページのサイトではなく、**1枚のページ（`docs/index.html`）の中で、設問ごとに独立した「問いカード」がそれぞれ状態を持つ**構成にする。ページ全体のナビゲーションや画面遷移は発生しない。

### ワイヤーフレーム（スマホ幅・設問2件の例）

```
┌───────────────────────────────┐
│  想起テスト                     │
│  2026-09-30                    │
├───────────────────────────────┤
│ Q1. 「会議は場で完結させる」      │  ← 問いカード（状態: pending）
│    （世界一流エンジニアの思考法）  │
│  適用の問い                      │
│  今週の実際の場面を1つ挙げ、      │
│  この考えをどう使うか            │
│  説明してください。               │
│                                 │
│  ┌───────────────────────────┐ │
│  │ (回答入力欄・複数行)          │ │
│  └───────────────────────────┘ │
│        [ 回答する ]              │
├───────────────────────────────┤
│ Q2. 「危機感がない病：…」         │  ← 別カード。Q1とは独立
│    （V字回復の経営）              │
│  まだ内容が確定していないカード    │
│  a) この考えを自分の言葉で1文     │
│  b) なぜ自分に重要か              │
│  c) 使う場面の具体的な状況         │
│        [ 記入する ]              │
└───────────────────────────────┘
```

補完（fill）タイプは、**最初からclaim／why／sceneの3つの入力欄を分けて**表示する（自由記述をあとから正規表現で分割する必要がなく、分割失敗というエラーパス自体が発生しない）。

### 問いカードの状態遷移

```
pending（未回答・入力可）
   │ 回答を入力して送信ボタン押下
   ▼
submitting（ボタン無効化・「送信中…」表示、入力内容は保持）
   │                         │
   │ 成功（ok: true）          │ 失敗（通信エラー／ok: false）
   ▼                         ▼
result                    error
（合否・点数・            （エラー文言を表示し、ボタンを
 フィードバック表示。       「再送信」に戻す。入力は消さない）
 fillは「記入ありがとう      │
 ございます」のみ）          └──────────▶ submitting へ戻る
```

- `result` の内容は localStorage に保存し、再読み込み時はフォームの代わりに結果を表示する（再送信はしない。誤答の再挑戦は翌日以降の通常の出題サイクルに任せる）
- 全カードが回答済みなら、ページ下部に「今日の分はすべて回答済みです。」を出す

## 7. データの流れ（詳細シーケンス）

```
[0] 毎朝08:00 JST（daily-question.yml）
    出題カードを選定 → docs/today.json・docs/index.html を生成
    → 通知専用Issue「想起テスト YYYY-MM-DD」を作成して即close（本文はPagesリンク）
    → docs/ に差分があれば recall-bot 名義で commit・push → GitHub Pagesに反映

[1] ページ読み込み
    ブラウザ ─ GET today.json?_=<timestamp> ──▶ GitHub Pages（静的ファイル）
    ブラウザのJSが localStorage を見て、今日すでに回答済みのcardは
    result表示（保存済みの点数・フィードバックを再表示）にする

[2] 回答送信（未回答のカードのみ）
    ブラウザ ─ POST https://mybrain-cards.vercel.app/api/grade ──▶ Vercel関数
              Body: {date, card, qtype, answers}
              （fillタイプは answers が {claim, why, scene}、それ以外は {text}）
    CORS：関数は Access-Control-Allow-Origin: https://horiken1977.github.io を返す

[3] 関数内の処理（api/grade.py）
    a. GitHub Contents API
       GET /repos/horiken1977/mybrain-cards/contents/{card}?ref=main
       → { content(base64), sha }（パスは urllib.parse.quote でエンコード）
    b. frontmatter/本文をパース
    c. qtype=fill：主張／なぜ自分に重要か／使う場面 を answers で置き換え
       （3欄のどれかが空なら incomplete_answer で更新しない）
       qtype!=fill：Anthropic Messages API で4軸採点＋feedbackを取得
       （回答が空なら empty_answer で更新しない）
    d. frontmatter（status/streak/last_reviewed/next_review）と回答履歴を更新
    e. GitHub Contents API
       PUT /repos/horiken1977/mybrain-cards/contents/{card}
       Body: {message: "recall(web): update card state for <date>",
              content(base64), sha, branch: "main",
              committer: recall-bot <actions@users.noreply.github.com>}

[4] レスポンス
    関数 ─ JSON ──▶ ブラウザ
    ブラウザはそのカードを result 表示にし、ok のときだけ localStorage に保存

[5] Mac側との同期
    `/card`・`/recall` 実行時に git pull / push し、関数がcommitした
    最新のカード状態をMac側にも反映する
```

## 8. データの格納方法

### `docs/today.json`（daily-question.ymlが毎日生成・上書き）

```json
{
  "date": "2026-09-30",
  "questions": [
    {
      "card": "会議は場で完結させる.md",
      "qtype": "apply",
      "title": "会議は場で完結させる",
      "book": "世界一流エンジニアの思考法",
      "text": "今週の実際の場面を1つ挙げ、その場面でこの考えをどう使うか説明してください。",
      "fields": ["text"]
    },
    {
      "card": "危機感がない病：業績と自分が紐づいていない.md",
      "qtype": "fill",
      "title": "危機感がない病：業績と自分が紐づいていない",
      "book": "V字回復の経営",
      "claim_draft": "大赤字でも危機感が生まれないのは、自分の仕事・給与・評価が業績と紐づいていないから。",
      "fields": ["claim", "why", "scene"]
    }
  ]
}
```

- `title`・`book`・`text`・`claim_draft` は生成時に HTML エスケープ済み
- `fields` は、その問いカードに何個・どんな入力欄を出すかをJS側に明示するためのフィールド
- 1日1ファイルで上書き。過去日のログは持たない（履歴が要る場合は各カードの`回答履歴`を見る）

### POSTリクエストのペイロード（ブラウザ→関数）

```json
{
  "date": "2026-09-30",
  "card": "会議は場で完結させる.md",
  "qtype": "apply",
  "answers": { "text": "来週の顧客提案ミーティングで、資料は最小限にして口頭中心で進める" }
}
```

fillタイプの場合は `answers` が `{ "claim": "...", "why": "...", "scene": "..." }`。

`date` はコミットメッセージにだけ使う。回答履歴・`last_reviewed` の日付は関数側の JST 当日で決まる。

### レスポンスのペイロード（関数→ブラウザ）

```json
// 採点あり（recall/apply/contrast/refute）— HTTP 200
{
  "ok": true,
  "type": "graded",
  "score": { "accuracy": 2, "example": 1, "conditions": 1, "action": 1, "total": 5 },
  "passed": true,
  "feedback": "具体的な適用例は良いが、逆に使えない条件にも触れるとさらに良い。",
  "new_status": "学習中",
  "next_review": "2026-10-03"
}

// 記入のみ（fill）— HTTP 200
{ "ok": true, "type": "fill" }

// 入力不備 — HTTP 200（カードは更新しない）
{ "ok": false, "error": "incomplete_answer" }   // または empty_answer / card_not_found

// 例外（GitHub/Anthropic API失敗など）— HTTP 500
{ "ok": false, "error": "<例外メッセージ>" }
```

### カードファイル本体（GitHubリポジトリ、Contents API経由で更新）

- 保存形式・frontmatterのスキーマは §4 の通り（mybrain の CLAUDE.md「定着レイヤー」参照）
- 1回の回答につき1コミット（`recall(web): update card state for <date>`、コミッター `recall-bot`）
- 同時更新の衝突（`sha` 不一致の409）は**未対応**。失敗はHTTP 500としてブラウザに返り、ユーザーが再送信する

### ブラウザ側の一時状態（localStorage）

- キー：`recall:<date>:<card>`（例：`recall:2026-09-30:会議は場で完結させる.md`）
- 値：関数からのレスポンス全体（JSON文字列）。`ok: true` のときだけ保存
- 用途：同じ日にページを再読み込みしても「回答済み」の表示を復元するためだけの**キャッシュ**。消えてもカード本体には影響しない＝正本はあくまでカードファイル
- 複数デバイス間では同期されない（PCとスマホで同じ日にアクセスすると、片方では「回答済み」表示が出ない）。これは許容する

## 9. 認証・秘密情報

| 秘密情報 | 置き場所 | 用途 |
|---|---|---|
| `ANTHROPIC_API_KEY` | Vercel環境変数（Production） | 採点 |
| `GITHUB_PAT` | Vercel環境変数（Production） | Contents APIでカードファイルを読み書き |
| `GITHUB_TOKEN`（Actions自動発行） | GitHub Actions | 通知Issueの作成・close、docs/ のpush |

**ブラウザ側（index.htmlのJS）とGitHubで管理するファイルには一切の秘密情報を置かない。** Vercel環境変数に置くことで、GitHubのsecret scanningがPATを検出して自動失効させる問題（§12.2）も避けている。

- GitHub リポジトリの Actions secret に `ANTHROPIC_API_KEY` が残っているが、旧方式（Actionsで採点）の名残で現在は使っていない（削除候補）
- `GITHUB_PAT` は「このrepo限定・Contents: Read/Write のみ」のFine-grained PATにする方針。実際の権限範囲は要確認

## 10. Mac側との同期

`/card`・`/recall`（mybrain側 `.claude/commands/`）は、`cards/` が独立git repoであることを前提に：

- 実行前に `git pull`
- 完了後に `git add -A && git commit && git push`

同一カードをMacとスマホで同日に編集すると衝突しうるため、基本はスマホの日次フローに任せ、Macは新規カード作成（`/card`）や重い振り返りに使う想定。

## 11. セキュリティ・プライバシー

- **2026-09-29**：`cards/` をmybrain本体（`raw/`の個人情報・キャリア情報等）から切り離した独立repoにする方針を決定（Git自体はセキュリティ上の懸念で一旦見送っていたが、cards専用の小さいリポジトリに限定する形で復活）
- **2026-09-30**：GitHub PagesがFreeプランではprivate repoに使えないため、`horiken1977/mybrain-cards` を**public**に変更。**カードのみを公開リポジトリに置く方式**として、読書ハイライト（本の引用）と自分の振り返り（「なぜ重要か」「使う場面」「回答履歴」）がインターネット上に公開される点は把握した上での判断
- 採点のため、回答テキストと該当カードの主張・根拠がClaude API（Anthropic）に送信される。コストは Haiku 4.5 使用で月数十〜数百円の想定
- 採点APIのURLは公開ページに載っており、CORSはブラウザからの呼び出しを制限するだけなので、URLを知っていれば誰でもPOSTできる（認証なし）。悪用されるとAPI費用とカードの書き換えが起こりうる点は既知のリスク
- mybrain本体（raw/の生資料・career/finance等）はこのリポジトリに一切含まれない

## 12. 実装の経緯

### 12.1 変遷（すべて2026-09-30）

| 版 | 回答の場所 | 採点の実行場所 | 状態 |
|---|---|---|---|
| v1 | GitHub Issueのコメント（`A1.`等の番号付き） | GitHub Actions（`issue_comment`→`grade-response.yml`） | 廃止 |
| v2a | Pagesのフォーム | GitHub Actions（`repository_dispatch`→`grade-web-response.yml`） | 廃止 |
| v2b | Pagesのフォーム | Vercel関数（`api/grade.py`） | **現行** |

### 12.2 変更理由

- **v1 → v2**：Issueへ移動して回答する2段構えのUXが事故を起こした
  1. 同名Issueの取り違え：同日に同じタイトルのIssueが複数でき、close済みの古いIssueに返信してしまう事故が複数回発生
  2. 番号なし回答の誤割当てバグ：複数設問のIssueで番号を付けない回答を質問1への回答と誤解釈し、無関係なカードを上書き
  - v2はフォーム送信時に`card`/`qtype`を一緒に送るため、「どの問いへの回答か」の取り違えが構造的に起こらない
- **v2a → v2b**：`repository_dispatch` をブラウザから呼ぶにはページにGitHub PATを埋め込む必要があり、public repoではGitHubのsecret scanningが検出してPATを自動失効させる（fine-grained/classic を問わず）。秘密情報をGitHub外に置くにはサーバーレス関数が必要になり、ユーザーの判断で **Vercel** を採用した（2026-09-30 15:58 JST の会話で決定）。秘密情報はVercel環境変数に置き、回答の受付→Claude APIで採点→結果をその場でユーザーに返す流れを同期処理で行う。ポーリングも不要になった
- 設計段階ではCloudflare Workersを推奨していたが、上記の判断でVercel（Python）に変更した

### 12.3 旧方式の後片付け

- `grade-response.yml`・`grade_and_update.py`（v1）、`grade-web-response.yml`・`grade_web_response.py`（v2a）は削除済み
- Issueは「通知専用」として残している（作成→即close。誰もコメントしない）。`recall` ラベルは使っていない
- 一時的なデバッグ用ワークフロー（`Debug secret length`）は削除済み

### 12.4 現在の運用フェーズ：MVP（2026-09-30〜）

2026-09-30 に「小さく始めるため、1問だけでテストする」と決め、1日1問のMVPで運用している（2026-10-01 に定時実行の既定値も1問に変更）。

| 確認項目 | 状態（2026-10-01時点） |
|---|---|
| 出題（Pages生成・通知メール） | 手動実行では確認済み。定時実行（cron）はまだ一度も動いていない |
| 補完（fill）の回答→カード更新 | Vercel経由で確認済み（09-30、`23948d9`） |
| 採点（graded）の回答→採点→カード更新 | **未確認**。09-30のテストで Anthropic API のクレジット残高不足（`credit balance is too low`）により失敗。クレジット追加後に再確認が必要 |

未記入カードが残っている間は fill の問いが優先されるため、採点APIを使わずに回せる。未記入カード（09-30時点で3枚）がなくなると graded の問いになり、クレジットが必要になる。

**3問に増やす目安**：定時実行が数日続けて動くこと、graded の採点〜カード更新が実際に通ること。

## 13. 未決定事項

- 過去の回答履歴をページ上でどこまで見せるか（直近だけ／全履歴／`dashboard.base`相当のビューを別途作るか）
- 通知を今の「Issue作成→即close」のままにするか、別の手段（メール送信等）にするか
- 採点APIに簡易な認証（共有トークン等）を付けるか（§11の既知リスク）

## 14. 未実装・TODO

[`TODO.md`](TODO.md) を参照。
