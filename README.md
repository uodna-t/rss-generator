# 更新情報フィード生成

ブログやお知らせページの更新情報を、Atom / RSS として生成し、GitHub Pages で公開します。
GitHub Actions が定期実行します。公開リポジトリなら無料枠で動きます。
Python のパッケージ管理には [uv](https://docs.astral.sh/uv/) を使います。

現在の対象: [モビリタからのお知らせ](https://www.toyota.co.jp/mobilitas/news/)

## 仕組み

1. Playwright (Chromium) でページを開き、JavaScript の描画後の HTML を取得
2. 記事リンクを抽出(自動検出、または CSS セレクタ指定)
3. 状態ファイル `state.json` と突き合わせ、過去に見た記事も保持(サイトから消えてもフィードに残る)
4. `public/` に `<id>.atom.xml` と `<id>.rss.xml` を出力し、GitHub Pages に公開
5. `state.json` を **`feed-state` ブランチ** にコミット

### ブランチの使い分け

| ブランチ | 内容 | 更新するのは |
|---|---|---|
| `master` | コード・設定(`build_feeds.py`、`sites.yml` など) | 人間(プルリクエスト経由) |
| `feed-state` | `state.json` だけ | GitHub Actions(自動) |

Actions は `master` に push しません。そのため `master` を「プルリクエスト必須」に保護しても、
バイパス設定は不要です。`feed-state` ブランチは初回の実行時に自動で作られます。

## セットアップ

1. 手元で依存関係を固定します(`uv.lock` が作られるので、これもコミットします)。

   ```
   uv lock
   ```

2. このフォルダの内容を **公開リポジトリ** にコミットします。

   ```
   .github/workflows/build-feeds.yml
   .gitignore
   build_feeds.py
   pyproject.toml
   uv.lock
   sites.yml
   README.md
   ```

   `state.json` は含めません(`feed-state` ブランチで管理されます)。

3. GitHub の **Settings → Pages → Build and deployment → Source** を **GitHub Actions** にします。
4. **Actions → build-feeds → Run workflow** を `debug` にチェックを入れて手動実行します。
5. 成功すると、次の URL で購読できます。

   ```
   https://<ユーザー名>.github.io/<リポジトリ名>/mobilitas-news.atom.xml
   https://<ユーザー名>.github.io/<リポジトリ名>/mobilitas-news.rss.xml
   ```

   リポジトリ名が `<ユーザー名>.github.io` の場合は、ワークフローの `FEED_BASE_URL` を
   `https://<ユーザー名>.github.io/` に直してください。

## master ブランチの保護(ルールセット)

初回の実行が成功してから設定すると、動作確認で詰まりません。

**Settings → Rules → Rulesets → New ruleset → New branch ruleset**

- Enforcement status: **Active**
- Bypass list: **空のまま**(Actions は `master` に push しないため不要)
- Target branches: Add target → Include by pattern → `master`
  (`feed-state` を含めないでください。「All branches」にすると Actions の push が拒否されます)
- Rules:
  - **Require a pull request before merging**(一人で運用するなら、必要な承認数は 0)
  - **Block force pushes**
  - **Restrict deletions**

## 手元で動かす

```
uv sync
uv run playwright install chromium           # 初回のみ
uv run build_feeds.py                        # 実際にサイトへアクセスして生成
uv run build_feeds.py --from-html page.html  # 保存済みHTMLで試す(ブラウザ不要)
```

手元で出力される `state.json` は `.gitignore` で除外されています。

ライブラリを追加・更新するときは `uv add <パッケージ>` や `uv lock --upgrade` を使い、
`pyproject.toml` と `uv.lock` をプルリクエストで反映します。

## 記事が正しく拾えないとき

初回は特に、サイト構造に合わせた調整が必要になる場合があります。

- 失敗したときや `debug` 付きで実行したときは、描画後の HTML が成果物 `debug-html` に保存されます。
  ダウンロードして記事一覧の部分を探し、`sites.yml` の `selectors` を指定してください。
- 自動検出は、`link_prefixes` で始まるリンクを記事とみなし、周辺の文字列から日付を探します。
  日付が見つからない記事は、初めて見た時刻を日付代わりに使います。
- 一覧が遅れて表示されるサイトは、`wait_for` に記事リンクのセレクタを指定します。

## 運用上の注意

- **60日ルール**: 公開リポジトリでは、60日間「動き」がないと定期実行が止まります。
  ワークフローは、`feed-state` ブランチの最後のコミットから45日以上空くと空コミットを入れます。
  別ブランチへのコミットがこの「動き」に数えられるかは、公式の記述を確認できていません。
  運用開始後しばらくは、Actions が無効化されていないか確認してください。
  無効化されていたら、Actions タブの「Enable workflow」で復活できます。
- **失敗時**: 抽出に失敗すると、過去の記事から生成したフィードを公開したうえで、ジョブを失敗扱いにします。
  失敗通知は GitHub から届きます。
- **状態のリセット**: フィードの履歴を消したいときは、`feed-state` ブランチを削除します。次回の実行で作り直されます。
- **マナー**: 実行間隔は3時間です。対象サイトの利用規約と robots.txt を確認し、頻度は必要以上に上げないでください。
- **サイトの追加**: `sites.yml` の `sites:` に項目を足すだけです(`master` へはプルリクエストで)。

## 公開されるデータについて

公開リポジトリと Pages の内容は誰でも見られます。生成するのは記事のタイトル・URL・日付だけで、
本文は含みません。
