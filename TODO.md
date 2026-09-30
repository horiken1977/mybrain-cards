# TODO

## 3. v2: サイト内で回答・採点・結果表示を完結させる（対応中・最優先）
- 設計は [`design/README.md`](design/README.md) §12 に確定済み。**実装はまだ**
- 概要：GitHub Issueへの移動をやめ、Webサイト（index.html）のフォームで回答→サーバーレス関数（Cloudflare Workers推奨）がClaude APIで採点→同じページに結果表示
- 実装前に決めること：サーバーレス関数のホスティング先アカウント作成、GitHub Fine-grained PAT発行
- 完了したら v1 のGitHub Issue関連（`daily-question.yml`のissue作成部分、`grade-response.yml`、`recall`ラベル）を削除する

## 2. GitHub Pagesで出題を見やすくする（v1・実装済み）
- 2026-09-30に実装・動作確認済み。v2完成まではこの仕組みで運用する

## 1. 正答率ベースの出題優先度（未着手）
- カードごとに正答率（合格/出題回数）を記録する
- 出題選定（`select_and_post.py` の `select_cards`）で、正答率が低いカードを優先する
- 現状の優先度は `priority`（高/中/低）＋期限超過日数のみで、正答率は見ていない
- `回答履歴` から正答率を集計するロジックが必要（例: 直近N回の合格率、または全期間の合格率）
- v2（サーバーレス関数）実装後に着手する方が、採点結果の保存先が一本化されて楽になる見込み
