# X 自動投稿システム

`posts.txt` から投稿文をランダムに1件選び、X API v2 の `POST /2/tweets` へOAuth 1.0aユーザーコンテキストで投稿します。GitHub ActionsがAsia/Tokyoの毎日8:10、12:10、20:10に実行し、手動実行にも対応します。

## 仕組み

- 投稿文は `posts.txt` の空行ではない各行から選択します。
- 同一内容の重複行は1件として扱います。
- 投稿成功時に本文のSHA-256ハッシュを `.state/last_post.sha256` へ保存します。
- GitHub Actionsのキャッシュから前回のハッシュを次回実行時に復元し、同じ文を候補から除外します。
- ワークフローの同時実行を禁止し、並行実行による連続重複を防ぎます。
- APIエラー時はHTTPステータス、Xのエラー本文、リクエストID、レート制限情報を可能な範囲でログへ出します。認証情報はログへ出しません。

> [!IMPORTANT]
> 連続重複を防ぐため、`posts.txt` には異なる投稿文を2件以上用意してください。GitHub Actionsのキャッシュが削除・期限切れになった場合、前回状態は復元できません。

## 1. X Developer Portalの準備

1. X Developer PortalでProjectとAppを作成します。
2. Appの認証設定で投稿を許可する権限（Read and Write）を設定します。
3. OAuth 1.0aのAPI Key / Secretと、投稿対象アカウントのAccess Token / Secretを生成します。

権限を変更した場合は、Access TokenとAccess Token Secretを再生成してください。X APIの利用プラン、使用量上限、投稿に関するポリシーも事前に確認してください。

## 2. GitHub Secretsの登録

GitHubリポジトリの **Settings → Secrets and variables → Actions → New repository secret** から、次の4件を登録します。

| Secret名 | 設定する値 |
| --- | --- |
| `X_API_KEY` | AppのAPI Key |
| `X_API_SECRET` | AppのAPI Key Secret |
| `X_ACCESS_TOKEN` | 投稿対象ユーザーのAccess Token |
| `X_ACCESS_TOKEN_SECRET` | 投稿対象ユーザーのAccess Token Secret |
| `GEMINI_API_KEY` | Gemini APIキー（AI利用時のみ） |

認証情報を `posts.txt`、Pythonソース、ワークフローなどへ直接記載しないでください。

## AI投稿（安全な段階導入）

GitHub Actionsは既定で `AI_DRY_RUN=true` として動作します。朝・昼・夜の時間帯に合わせてGemini API（`gemini-3.1-flash-lite`）で30〜100文字の日本語文を生成し、直近30件の履歴と完全一致・類似度を確認します。最大3回再生成しても条件に合わない場合やAI APIが失敗した場合は、`posts.txt`の固定文へフォールバックします。AIレスポンス全文やキーはログへ出しません。

手動dry-runは **Actions → Post to X → Run workflow** で `ai_dry_run=true` のまま実行します。最終候補だけがログに表示され、Xへは投稿しません。実投稿へ切り替える場合は `ai_dry_run=false` を選びます。スケジュール実行は常にdry-runです。

AIを使わず固定文だけで運用する場合は、`GEMINI_API_KEY`を登録せず、手動実行の`ai_dry_run=false`（または環境変数`AI_DRY_RUN=false`）で実行してください。AI生成を試みず、`posts.txt`から選択します。Gemini APIは無料枠・レート制限・仕様が変更される可能性があるため、Google AI Studioの最新の利用条件を確認してください。

## 3. 投稿文の編集

`posts.txt` をUTF-8で編集し、1行につき1投稿を記載します。空行は無視されます。

```text
おはようございます。今日も良い一日を。
お昼になりました。午後も頑張りましょう。
今日も一日お疲れさまでした。
```

複数行の投稿には対応していません。投稿可能な文字数や内容の制限はXの仕様に従います。リポジトリに含まれるサンプル文は、運用開始前に実際の投稿文へ置き換えてください。

## 4. 自動・手動実行

`.github/workflows/post-to-x.yml` は次の時刻にAsia/Tokyo指定で起動します。

- 毎日 8:10
- 毎日 12:10
- 毎日 20:10

スケジュール実行はデフォルトブランチ上のワークフローが対象です。GitHub側の混雑により開始が遅れる場合があります。

手動実行はGitHubの **Actions → Post to X → Run workflow** から行えます。初回はSecretsと投稿文を設定した後、手動実行で動作確認することを推奨します。

## ローカルでの確認

投稿せずに単体テストだけ実行する手順です。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

`python post_to_x.py` は実際に投稿します。ローカル実行する場合も4つの認証情報を環境変数として設定してください。`.env` ファイルは自動では読み込まず、Gitでも無視されます。

## エラーの確認

GitHubの **Actions → 対象の実行 → Post to X** でログを確認します。主な原因は次のとおりです。

- `必要な認証情報が設定されていません`: GitHub Secretsの名前または値を確認します。
- `HTTP 401`: キーやトークンが無効、失効、または異なるAppの組み合わせです。
- `HTTP 403`: AppがRead and Writeではない、または利用プラン・ポリシー上の権限がありません。
- `HTTP 429`: APIのレート制限または使用量上限です。ログのリセット時刻も確認します。
- `別の投稿文がありません`: `posts.txt` に異なる文を2件以上追加します。
- `Gemini APIエラー` / `AI生成を3回試行`: `GEMINI_API_KEY`、無料枠・レート制限、モデル利用可否を確認し、固定文フォールバックでの動作を確認します。

通信タイムアウト後は、X側で投稿だけ成功している可能性があります。意図しない重複を避けるため自動再試行は行いません。X上の投稿状況を確認してから再実行してください。

## ファイル構成

```text
.
├── .github/workflows/post-to-x.yml  # 実行スケジュールとSecretsの受け渡し
├── post_to_x.py                     # AI生成、固定文フォールバック、投稿、状態保存
├── posts.txt                        # 投稿候補（1行1投稿）
├── requirements.txt                 # Python依存パッケージ
└── tests/test_post_to_x.py          # APIを呼ばない単体テスト
```
