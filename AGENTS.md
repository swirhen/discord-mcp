# AI開発エージェント向け運用ルール

## プロジェクト概要

- 複数の Discord サーバーを 1 つの MCP サーバーで扱う (slack-mcp の Discord 版)。自宅サーバーで動かす。
- リポジトリは公開している。README は任意の Linux サーバーで構築する前提で書いている (URL は `https://example.com/...`、配置先は `/opt/discord-mcp` などの例)。うちの環境に固有の値は README に書かず、この AGENTS.md の「うちの環境」に書く。
- Discord はユーザートークンでの自動操作を規約で禁止しているので、**ボットのトークン**で REST API (v10) を呼ぶ。ユーザートークン (セルフボット) 対応は足さない。
- アカウント = ボット。トークンは `accounts/<アカウント名>.toml` (Git 管理外)。1 つのボットで招待したサーバー全部を扱い、`servers` で扱うサーバーを絞れる。各ツールの `account` でボットを、`server` でサーバー (ID か名前) を選ぶ (1 つだけなら省略できる)。
- AI ごとにコネクタの URL を分ける。設定は `clients.json` (Git 管理外。`clients.py` で追加・変更・作り直し。サーバーは毎回ファイルを確かめるので再起動は要らない)。AI ごとに `post_as` (webhook / bot)、表示名・アイコン、使えるアカウント、読み取り専用を持つ。
- `post_as = "webhook"` の AI は、チャンネル (スレッドなら親) にこのアプリの webhook を作って、AI の名前で投稿する。webhook でできないもの (DM・リアクション・返信) はボットで行うか、返信先のリンクを本文に付ける。webhook を作れないときはボットで送り直さずにエラーにする。
- MCP サーバー (`mcp_server/server.py`) は systemd の `discord-mcp.service` (`127.0.0.1:8011`) で動かし、リバースプロキシの `.../mcp/` で公開する。コネクタの URL は `<DISCORD_MCP_PUBLIC_URL>/<トークン>`。`DISCORD_MCP_PUBLIC_URL` が空なら `connector_url` は相対パス `../mcp/<トークン>` を返し、管理画面 (`fullUrl`) が画面の URL をもとに完全な URL に直す。入口で URL のトークンから AI を決め、`x-discord-mcp-client` ヘッダーに付け直してツールに渡す (外から付けたものは捨てる)。
- Gateway には接続しない (discord.py は使わない)。ツールが呼ばれたときだけ REST を呼ぶ。
- 管理画面 (`web/api.py` の FastAPI が `web/index.html` を配る。ビルドなし) は `discord-mcp-admin.service` (`127.0.0.1:8012`) で動かし、リバースプロキシの `.../admin/` で公開する。AI のコネクタ (`clients.json`) と `accounts/*.toml` を書き、接続テストでボットの intent・入っているサーバー・サーバーごとの足りない権限を出す。ログイン情報は本番 `/etc/default/discord-mcp-admin`、ローカル `idpass.txt`。画面は相対パス (`api/...`) で API を呼ぶので、どのパスの下でも動く。

## 主なファイル

- `mcp_server/server.py`: ツールの定義 (Streamable HTTP の MCP サーバー)、`/mcp/<トークン>` の確認、接続元の AI の判定、`Poster` (webhook とボットの投稿の切り替え)。
- `bot.py`: アカウントごとの状態 (自分・サーバー・チャンネル・ロール・webhook のキャッシュ、`servers` による制限) と、メッセージの整形、時刻・snowflake の変換。
- `discord_client.py`: Discord REST API の薄いクライアント (httpx)。429 の再試行、202 (準備中) の扱い、multipart、添付の取得。webhook の実行はトークンを空にして認証ヘッダーなしで呼ぶ。
- `bot.py` の `PERMISSIONS` が招待 URL に含める権限と、管理画面で確かめる権限の両方の元。権限を足すときはここだけ変える。
- `accounts.py`: `accounts/*.toml` の読み込み (変更を検知して読み直す) と、管理画面が使う `write_account` / `delete_account`。
- `clients.py`: AI ごとのコネクタの設定の読み書き (`create_client` などは管理画面も使う) と、管理のコマンド (`add` / `set` / `list` / `url` / `regenerate` / `remove`)。
- `web/api.py` / `web/index.html`: 管理画面の API と画面。
- `mcp_server/discord-mcp.service` / `web/discord-mcp-admin.service`: systemd 定義。README では「例」として扱っている (下の「うちの環境」の値がそのまま入っている)。
- `tests/fake_discord.py` / `tests/smoke.py`: 偽の Discord API に対する通し試験 (コネクタ tester=bot、claude=webhook、viewer=読み取り専用)。
- `tests/admin_test.py`: 管理画面の API の試験 (偽の Discord API と FastAPI の TestClient)。

## 開発と検証

- Python の venv はリポジトリ外の `~\.venv\discord-mcp` に PC ごとに作る (ソースは OneDrive で PC 間共有するため)。
  作成: `python -m venv $env:USERPROFILE\.venv\discord-mcp; & $env:USERPROFILE\.venv\discord-mcp\Scripts\python.exe -m pip install -r requirements.txt`
- 本番は Ubuntu 22.04 (Python 3.10)。3.11 以降の機能 (`tomllib` 直接、f-string の中で同じ引用符を使う書き方など) に依存しないこと。
- 変更後は `python -m py_compile accounts.py discord_client.py bot.py clients.py mcp_server/server.py web/api.py` と、通し試験 `python tests/smoke.py`・管理画面の試験 `python tests/admin_test.py` (どちらも実際の Discord には接続しない) を実行する。
- `discord_client.http()` はイベントループごとに HTTP クライアントを持つ (管理画面の試験は MCP サーバーと別のループから呼ぶため)。
- ツールを増やしたら、引数の先頭に `ctx: Context` を付け (接続元の AI の判定に使う)、`tests/smoke.py` のツール数の確認と、偽サーバー (`tests/fake_discord.py`) の対応する経路も足す。
- チャンネルを受け取るツールは必ず `bot.channel()` を通す (`servers` の制限をそこで確かめている)。サーバーを受け取るツールは `bot.guild()` を通す。送信・変更系は `poster()` か `writable()` を通す (コネクタとアカウントの読み取り専用をそこで確かめている)。
- HTTP ヘッダーに日本語を入れない (監査ログの理由などは URL エンコードする)。

## 注意

- `accounts/`・`clients.json`・`idpass.txt`・`/etc/default/discord-mcp-admin` は認証情報を含む。内容を表示・コミットしない。
- 発言はボットか AI の名前でサーバーの人に見える。試験は `tests/smoke.py` (偽サーバー) で行い、本物のサーバーにテスト投稿をしない。
- Discord API の制約: メッセージ本文の取得・本文検索には Message Content Intent、メンバーの一覧には Server Members Intent が要る (Developer Portal でオン)。webhook には「ウェブフックの管理」の権限が要り、1 チャンネル 15 個まで。検索 (`/guilds/{id}/messages/search`) はインデックスの準備中に 202 を返す。予約送信・下書きの API はない。1 メッセージ 2000 文字まで、添付はブーストなしで 10 MB まで。
- README やコード・試験に、うちの環境の値 (ドメイン・ホスト名・パスなど) を書かない (systemd 定義の例は除く)。例は `example.com`・`/opt/discord-mcp` にする。

## うちの環境

- サーバー: `prod-checker` (SSH の Host 名。Ubuntu 22.04)。配置先は `/home/swirhen/discord-mcp` (README の `/opt/discord-mcp` に当たる)。動かすユーザーは `swirhen`。venv はリポジトリ直下の `.venv`。
- 公開 URL: MCP は `https://swirhen.tv/discord_mcp/mcp/<トークン>` (`127.0.0.1:8011` へ)、管理画面は `https://swirhen.tv/discord_mcp/admin/` (`127.0.0.1:8012` へ)。MCP は管理画面と同じ階層なので、`DISCORD_MCP_PUBLIC_URL` は設定していない (管理画面が URL を組み立てる)。本番で `clients.py` の URL を完全な形で見たいときは `DISCORD_MCP_PUBLIC_URL=https://swirhen.tv/discord_mcp/mcp` を付けて実行する。
- リバースプロキシ: Apache。設定は dotfile リポジトリの `apache2/sites-available/000-default.conf` にあり、本番の `/etc/apache2/sites-available/000-default.conf` はそこへのシンボリックリンク。本番の `~/dotfile` で `git pull` してから `sudo apachectl configtest && sudo systemctl reload apache2` で反映する。
- systemd: `discord-mcp.service` と `discord-mcp-admin.service`。リポジトリの定義ファイルをそのまま `/etc/systemd/system/` に入れている。
- 管理画面のログイン情報: `/etc/default/discord-mcp-admin` (root・600)。

### 更新のリリース

GitHub へプッシュした後:

```powershell
ssh prod-checker "cd /home/swirhen/discord-mcp && git pull --ff-only && sudo systemctl restart discord-mcp.service discord-mcp-admin.service && systemctl is-active discord-mcp.service discord-mcp-admin.service"
```

- 依存パッケージを変えたときは、再起動の前に `.venv/bin/pip install -r requirements.txt` も実行する。
- systemd の定義を変えたときは、`sudo install -m 0644 mcp_server/discord-mcp.service web/discord-mcp-admin.service /etc/systemd/system/ && sudo systemctl daemon-reload` もやり直す。
- 初回の導入手順は `README.md` の「導入」を、うちの環境の値に読み替えて行った。
