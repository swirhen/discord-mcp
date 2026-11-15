# discord-mcp

複数の Discord サーバーを 1 つの MCP サーバーで扱う。slack-mcp の Discord 版で、自宅サーバーで動かす。
AI (claude.ai・ChatGPT など) ごとにコネクタの URL を分け、AI ごとの名前で投稿できる。

- リポジトリ: `swirhen/discord-mcp` (非公開)
- 本番の配置先: `/home/swirhen/discord-mcp`
- 公開 URL: Apache 配下の `/discord_mcp/mcp/<AI ごとのトークン>` (`127.0.0.1:8011` へ転送)
- 管理画面: `https://swirhen.tv/discord_mcp/admin/` (`127.0.0.1:8012` へ転送。AI のコネクタとボット・扱うサーバーをここで設定する)

Discord はユーザートークンでの自動操作 (セルフボット) を規約で禁止しているので、**ボットとして**読み書きする。

- 対象は、ボットを招待したサーバーと、その中でボットに見る権限があるチャンネルだけ
- 本人の DM やフレンドは読めない。ボットへの DM・ボットからの DM は扱える
- 招待にはそのサーバーの「サーバー管理」の権限が要る (管理者でないサーバーは、管理者に招待 URL を渡して入れてもらう)

---

## 全体像

```
 claude.ai  ──▶ https://swirhen.tv/discord_mcp/mcp/<claude のトークン>  ─┐
 ChatGPT    ──▶ https://swirhen.tv/discord_mcp/mcp/<chatgpt のトークン> ─┤
                                                                        ▼
                      Apache ──▶ mcp_server/server.py (127.0.0.1:8011, systemd: discord-mcp.service)
                                   │ clients.json でトークンから AI を決める (表示名・投稿のしかた・使えるアカウント)
                                   ├─ accounts/main.toml (ボット A) ──▶ サーバー 1, サーバー 2, …
                                   └─ accounts/<名前>.toml (ボット B) ──▶ …
```

Gateway (WebSocket) には常時接続せず、ツールが呼ばれたときだけ REST API を呼ぶ。同じボットがほかで常駐していてもぶつからない。

| ファイル | 役割 |
| --- | --- |
| `mcp_server/server.py` | MCP サーバー本体 (Streamable HTTP)。ツールの定義、URL のトークンからの AI の判定、webhook とボットの投稿の切り替え |
| `bot.py` | アカウント (ボット) ごとの状態 (自分・サーバー・チャンネル・ロール・webhook のキャッシュ、扱うサーバーの制限) と、メッセージの整形 |
| `discord_client.py` | Discord REST API (v10) の薄いクライアント (レート制限の再試行、multipart、添付の取得) |
| `accounts.py` | `accounts/<アカウント名>.toml` の読み込み |
| `clients.py` | AI ごとのコネクタの設定 (`clients.json`) の読み書きと、管理のコマンド |
| `web/api.py` / `web/index.html` | 管理画面 (FastAPI と、それが配る 1 枚の HTML。ビルドは要らない) |
| `account.example.toml` | アカウントの設定ファイルの例 |
| `tests/` | 偽の Discord API に対する通し試験と、管理画面の API の試験 |

---

## アカウント (ボット)

アカウント (ボット) ごとに `accounts/<アカウント名>.toml` が 1 つ (Git 管理外。ボットのトークンを含む)。
**1 つのボットで、招待したサーバー全部を扱える**。ふつうはボット 1 つ (`accounts/main.toml`) で足りる。
読み取り専用のボットを別に持ちたい、扱うサーバーを分けたい、といったときにファイルを増やす。

```toml
bot_token = "MTAw…"                       # Developer Portal の Bot ページのトークン
label = "MCP 用のボット"                   # 任意。一覧に出す説明
read_only = false                         # 任意。true なら送信・変更系のツールを断る
servers = ["123456789012345678"]          # 任意。扱ってよいサーバーの ID (省略するとボットが入っているサーバー全部)
```

- アカウント名は英小文字・数字・ハイフン・アンダースコアで 32 文字まで。MCP の各ツールの `account` にこの名前を指定する (使えるアカウントが 1 つだけなら省略できる)
- 各ツールの `server` にはサーバーの ID か名前 (一意なら一部でもよい) を渡す (扱えるサーバーが 1 つだけなら省略できる)
- ファイルを足したり書き換えたりすると、再起動しなくても次の呼び出しから反映される

### ボットの作成と招待

1. https://discord.com/developers/applications で **New Application** (名前がボットの表示名の元になる)
2. 左の **Bot** で
   - **Reset Token** → 出たトークンを `accounts/<アカウント名>.toml` の `bot_token` に書く (トークンは一度しか表示されない)
   - **Privileged Gateway Intents** の **Message Content Intent** をオン (オフだとボット宛てでないメッセージの本文が空になり、本文の検索もできない)
   - **Server Members Intent** をオン (メンバーの一覧に要る。名前での検索だけなら無くても動く)
   - 他人に入れさせないなら **Public Bot** をオフ
3. サーバーに招待する。トークンを書いたあと MCP の `discord_list_accounts` を呼ぶと `invite_url` が出るので、それを開いてサーバーを選ぶ。
   手で作るなら **OAuth2 → URL Generator** で scope `bot`、権限は次のとおり (`permissions=309774896192`):
   View Channels / Send Messages / Send Messages in Threads / Create Public Threads / Embed Links / Attach Files /
   Read Message History / Add Reactions / Use External Emojis / Manage Webhooks
4. 非公開のチャンネルは、そのチャンネルの権限でボット (かボットのロール) に「チャンネルを見る」「メッセージ履歴を読む」を許可する

「ウェブフックの管理」(Manage Webhooks) は、AI の名前で投稿する (下の `post_as = "webhook"`) ために要る。
ボットの投稿だけでよい (すべての AI を `post_as = "bot"` にする) なら外してよい。

---

## 管理画面

`https://swirhen.tv/discord_mcp/admin/` でログインして、次のことができる。保存は再起動しなくても MCP サーバーにすぐ効く。

- **AI コネクタ**: AI の追加・編集・削除、コネクタ URL の表示・コピー・作り直し (表示名・アイコン・投稿方法・使えるボット・読み取り専用)
- **ボットとサーバー**: `accounts/<名前>.toml` の作成・編集・削除 (削除は `accounts/.deleted/` へ移す)。トークンは画面に表示せず、空のまま保存すると今のトークンを残す。
  接続テストで、ボットのユーザー、Message Content Intent・Server Members Intent がオンか、Public Bot か、ボットが入っているサーバーと
  そのサーバーで足りない権限 (ロールから計算したサーバー全体のもの) を確かめる。扱うサーバーを選んで絞れる。招待 URL を開く・コピーできる
- **ボットの作り方**: Developer Portal での手順と、招待に含める権限の一覧

ログイン情報は本番では `/etc/default/discord-mcp-admin` (root 所有・600)、ローカルではリポジトリ直下の `idpass.txt` (1 行目がユーザー名、2 行目がパスワード。Git 管理外)。

```
DISCORD_MCP_ADMIN_USERNAME=…
DISCORD_MCP_ADMIN_PASSWORD=…
DISCORD_MCP_ADMIN_SECRET=…    # Cookie の署名用。長いランダムな文字列 (openssl rand -hex 32)。変えると全員ログアウトする
```

- Cookie は HttpOnly・SameSite=Strict・Secure (本番)。書き込みの API は JSON しか受け付けない (他サイトからのフォーム送信を防ぐ)
- 同じ接続元からログインに 10 分で 10 回失敗すると、しばらく受け付けない
- ローカルで開くとき: `python -m uvicorn --app-dir web api:app --host 127.0.0.1 --port 8012` で `http://127.0.0.1:8012/`

---

## AI ごとのコネクタ (clients.json)

AI ごとにコネクタの URL を発行する。設定は `clients.json` (Git 管理外・600) にあり、管理画面か `clients.py` で管理する。
サーバーは毎回ファイルを確かめるので、追加・変更・作り直しに再起動は要らない。

```bash
python clients.py add claude --name Claude --avatar-url https://example.com/claude.png   # URL が表示される
python clients.py add chatgpt --name ChatGPT --accounts main
python clients.py add viewer --post-as bot --read-only
python clients.py list
python clients.py set chatgpt --post-as bot        # 変える
python clients.py url claude                        # URL を表示する
python clients.py regenerate claude                 # トークンを作り直す (前の URL はすぐ使えなくなる)
python clients.py remove viewer
```

| 設定 | 意味 |
| --- | --- |
| `name` | webhook で投稿するときの表示名 (80 文字まで。"discord" "clyde" "@" "#" ":" "```" を含む名前と "everyone" "here" は Discord が断るので、保存の時点で断る) |
| `avatar_url` | webhook で投稿するときのアイコン画像の URL |
| `post_as` | `webhook` (既定): AI の名前とアイコンで投稿する / `bot`: ボット自身の名前で投稿する |
| `accounts` | 使ってよいアカウント名 (空ならすべて) |
| `read_only` | 読み取りだけ (送信・変更系のツールを断る) |

### webhook での投稿 (`post_as = "webhook"`)

- 送るチャンネル (スレッドなら親のチャンネル) に、このアプリの webhook (`mcp-bridge`) を 1 つ作って使い回す。投稿ごとに AI の名前とアイコンを付ける
  (Discord は webhook の名前に "discord" を含めると断るので、リポジトリ名は使わない)
- 表示名に Discord が断る文字が入っていたとき (保存の検証より前に作った設定など) は、その部分を取り除いて投稿する。本文に "discord" と書くのは問題ない
- 対象はメッセージの送信 (`discord_send_message`)、ファイル (`discord_upload_file`)、スレッドの最初のメッセージ、フォーラムの投稿 (`discord_create_thread`)
- webhook は返信を作れないので、返信は `-# ↪ 返信: <返信先のリンク>` を先頭に付けて送る
- DM・リアクションは webhook ではできないので、ボットの名前になる。スレッドの作成者 (フォーラム以外) もボット
- 編集・削除は、ボット自身か、このアプリの webhook が送ったメッセージだけできる
- 「ウェブフックの管理」の権限がないチャンネルではエラーになる (ボットの名前で送り直しはしない)
- 誰かが webhook を消しても、次の投稿で作り直す

---

## ツール

| 種類 | ツール |
| --- | --- |
| アカウント・サーバー | `discord_list_accounts` (招待 URL と、このコネクタの設定つき), `discord_list_servers` |
| チャンネル | `discord_list_channels` (カテゴリ・進行中のスレッドつき), `discord_list_threads` (アーカイブ済みも) |
| メッセージを読む | `discord_read_channel` (チャンネル・スレッド・DM), `discord_get_message` (1 件の詳細), `discord_search_messages`, `discord_list_pins` |
| ユーザー | `discord_search_members`, `discord_read_user_profile` |
| 送る | `discord_send_message` (返信・2000 文字超は分割), `discord_send_dm`, `discord_edit_message` / `discord_delete_message` (自分のものだけ), `discord_add_reaction` (外すのも), `discord_get_reactions`, `discord_create_thread` (フォーラムの投稿も) |
| ファイル | `discord_read_file` (テキストはそのまま、画像は画像で返す), `discord_upload_file` (テキストか Base64。10 MB まで) |
| 絵文字 | `discord_list_emojis` |

- チャンネル・メッセージ・ユーザーは ID (数字) で指定する。スレッドもチャンネルの一種なので、スレッドの ID を `channel_id` に渡して読み書きする
- 時刻は日本時間の ISO 8601 で返す。`before` / `after` はメッセージ ID か ISO 8601 (タイムゾーンなしは日本時間)
- 本文の `<@id>` `<#id>` `<@&id>` `<:name:id>` `<t:…>` は名前や日時に置き換えて返す
- 送信は既定で `@everyone` / `@here` / ロールのメンションを通知しない (`notify_everyone` / `notify_roles` を true にしたときだけ通知する)
- 検索 (`discord_search_messages`) は Discord 側のインデックスを使う。作っている途中だと「待ってやり直す」というエラーになる
- Discord に予約送信・下書きはないので、そのツールは無い

---

## ローカルでの実行

```powershell
python -m venv $env:USERPROFILE\.venv\discord-mcp
& $env:USERPROFILE\.venv\discord-mcp\Scripts\python.exe -m pip install -r requirements.txt
& $env:USERPROFILE\.venv\discord-mcp\Scripts\python.exe tests\smoke.py        # 偽の Discord に対する通し試験
& $env:USERPROFILE\.venv\discord-mcp\Scripts\python.exe tests\admin_test.py   # 管理画面の API の試験
& $env:USERPROFILE\.venv\discord-mcp\Scripts\python.exe clients.py add local --name Local
& $env:USERPROFILE\.venv\discord-mcp\Scripts\python.exe mcp_server\server.py   # http://127.0.0.1:8011/mcp/<トークン>
```

環境変数: `DISCORD_MCP_PORT` (既定 8011)、`DISCORD_MCP_CLIENTS_FILE`、`DISCORD_MCP_PUBLIC_URL` (clients.py が表示する URL の前半)、
`DISCORD_MCP_ACCOUNTS_DIR`、`DISCORD_MCP_TZ` (既定 Asia/Tokyo)、`DISCORD_API_BASE` (テスト用)。

---

## 本番への導入 (Ubuntu 22.04)

ユーザー `swirhen`、配置先 `/home/swirhen/discord-mcp`、ポート `8011` (MCP) と `8012` (管理画面) を前提にしている。
アカウントと AI は管理画面で作れるので、下の `accounts/main.toml` と `clients.py add` は管理画面を使うなら飛ばしてよい。

```bash
cd /home/swirhen
git clone git@github.com:swirhen/discord-mcp.git
cd discord-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

mkdir -p accounts && chmod 700 accounts
cp account.example.toml accounts/main.toml && chmod 600 accounts/main.toml
vi accounts/main.toml                                # bot_token を書く

.venv/bin/python clients.py add claude --name Claude  # コネクタの URL が表示される (AI の数だけ)

sudo cp mcp_server/discord-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-mcp.service
systemctl is-active discord-mcp.service
```

管理画面:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/default/discord-mcp-admin
sudo vi /etc/default/discord-mcp-admin      # DISCORD_MCP_ADMIN_USERNAME / PASSWORD / SECRET を書く
sudo cp web/discord-mcp-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-mcp-admin.service
systemctl is-active discord-mcp-admin.service
```

Apache は `dotfile/apache2/sites-available/000-default.conf` に `/discord_mcp/mcp/` → `127.0.0.1:8011/mcp/` と、
`/discord_mcp/admin/` → `127.0.0.1:8012/` の転送がある。
本番の `~/dotfile` で `git pull` → `sudo apachectl configtest` → `sudo systemctl reload apache2`。

### claude.ai への登録

設定 → コネクタ → カスタムコネクタを追加 で、URL に管理画面でコピーした URL (か `python clients.py url claude`) を入れる (認証なし)。
URL を作り直したら、コネクタの URL も変える。

### 更新

```bash
ssh prod-checker "cd /home/swirhen/discord-mcp && git pull --ff-only && sudo systemctl restart discord-mcp.service discord-mcp-admin.service && systemctl is-active discord-mcp.service discord-mcp-admin.service"
```

---

## 安全のために

- コネクタの URL はトークンそのもの。漏れたら `python clients.py regenerate <id>` で作り直す (前の URL はすぐ使えなくなる)
- 接続元の AI は入口で URL のトークンから決める。外から付けた `x-discord-mcp-client` ヘッダーは捨てる
- ボットのトークンが漏れたら Developer Portal で Reset Token して `bot_token` を書き換える
- 発言はボットか AI の名前でサーバーの人たちに届く。信用しきれない AI のコネクタは `--read-only` にするか、`--accounts` で読み取り専用のアカウントだけにする
- ボットが入っているが AI に触らせたくないサーバーがあれば、アカウントの `servers` で扱うサーバーを絞る
- `accounts/`・`clients.json`・`idpass.txt`・`/etc/default/discord-mcp-admin` は Git 管理外。サーバーの外に出さない
