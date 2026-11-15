# discord-mcp

複数の Discord サーバーを 1 つの MCP サーバーで扱う。[slack-mcp](https://github.com/swirhen/slack-mcp) の Discord 版で、自分のサーバーで動かす。

- AI (claude.ai・ChatGPT など) ごとにコネクタの URL を分け、AI ごとの名前・アイコンで投稿できる (webhook)
- 管理画面から、AI のコネクタとボット・扱うサーバーを設定できる

Discord はユーザートークンでの自動操作 (セルフボット) を規約で禁止しているので、**ボットとして**読み書きする。

- 対象は、ボットを招待したサーバーと、その中でボットに見る権限があるチャンネルだけ
- 本人の DM やフレンドは読めない。ボットへの DM・ボットからの DM は扱える
- 招待にはそのサーバーの「サーバー管理」の権限が要る (管理者でないサーバーは、管理者に招待 URL を渡して入れてもらう)

> 日本語で使う前提で作っている (ツールの説明・管理画面・エラーは日本語。時刻の既定は日本時間)。

---

## 全体像

```
 claude.ai  ──▶ https://example.com/discord_mcp/mcp/<claude のトークン>  ─┐
 ChatGPT    ──▶ https://example.com/discord_mcp/mcp/<chatgpt のトークン> ─┤
                                                                         ▼
                      リバースプロキシ ──▶ mcp_server/server.py (127.0.0.1:8011)
                                   │ clients.json でトークンから AI を決める (表示名・投稿のしかた・使えるアカウント)
                                   ├─ accounts/main.toml (ボット A) ──▶ サーバー 1, サーバー 2, …
                                   └─ accounts/<名前>.toml (ボット B) ──▶ …

 ブラウザ ──▶ https://example.com/discord_mcp/admin/ ──▶ リバースプロキシ ──▶ web/api.py (127.0.0.1:8012)
                                                              clients.json と accounts/*.toml を書く
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
| `mcp_server/discord-mcp.service` / `web/discord-mcp-admin.service` | systemd の定義の例 |
| `account.example.toml` | アカウントの設定ファイルの例 |
| `tests/` | 偽の Discord API に対する通し試験と、管理画面の API の試験 |

実行中に、リポジトリの直下に次のものが作られる。どれも Git 管理外で、権限 600 (ディレクトリは 700) で書かれる。

| ファイル | 内容 |
| --- | --- |
| `accounts/<アカウント名>.toml` | ボットのトークン (アカウントごと) |
| `clients.json` | AI ごとのコネクタの設定 (URL のトークンを含む) |

---

## 必要なもの

- Linux などの常時動いているサーバー (Python 3.10 以上。例では systemd で動かす)
- HTTPS で公開できるリバースプロキシ (Apache・nginx など)。コネクタの URL にトークンを載せるので、**HTTP で公開しない**
- Discord のボット (Developer Portal で作る。下の「[ボットの作成と招待](#ボットの作成と招待)」)

---

## 導入

以下では、配置先を `/opt/discord-mcp`、公開する URL を `https://example.com/discord_mcp/mcp/` (MCP サーバー) と
`https://example.com/discord_mcp/admin/` (管理画面) として書く。自分の環境に合わせて読み替える。

### 1. 配置

```bash
sudo git clone https://github.com/swirhen/discord-mcp.git /opt/discord-mcp
sudo chown -R <動かすユーザー>: /opt/discord-mcp
cd /opt/discord-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. systemd に登録する

`mcp_server/discord-mcp.service` (MCP サーバー) と `web/discord-mcp-admin.service` (管理画面) は例。
`User`・`Group`・`WorkingDirectory`・`ExecStart` のパスを、自分の環境に合わせて書き換えてから登録する。

管理画面のログイン情報を、root だけが読めるファイルに書く:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/default/discord-mcp-admin
sudoedit /etc/default/discord-mcp-admin
```

```
DISCORD_MCP_ADMIN_USERNAME=…
DISCORD_MCP_ADMIN_PASSWORD=…
DISCORD_MCP_ADMIN_SECRET=…    # Cookie の署名用。長いランダムな文字列 (openssl rand -hex 32)。変えると全員ログアウトする
```

```bash
sudo install -m 0644 mcp_server/discord-mcp.service web/discord-mcp-admin.service /etc/systemd/system/   # 書き換えてから
sudo systemctl daemon-reload
sudo systemctl enable --now discord-mcp.service discord-mcp-admin.service
systemctl is-active discord-mcp.service discord-mcp-admin.service
```

MCP サーバーは `127.0.0.1:8011` (`DISCORD_MCP_PORT` で変えられる)、管理画面は `127.0.0.1:8012` (service の `--port`) で待ち受ける。

### 3. リバースプロキシで公開する

`/discord_mcp/mcp/` を `http://127.0.0.1:8011/mcp/` に、`/discord_mcp/admin/` を `http://127.0.0.1:8012/` に中継する。

- `X-Forwarded-For` を付ける。管理画面のログインの失敗の回数制限を、接続元ごとに数えるのに使う
- MCP サーバーには、`Host` ヘッダーを**書き換えて** (中継先の `127.0.0.1:8011` にして) 渡す。元の `Host` のまま渡すと、
  MCP の SDK の DNS リバインディング対策で拒否される。Apache の `ProxyPass` と nginx の `proxy_pass` は、既定でそうなる
- 管理画面は相対パスで API を呼ぶので、どのパスの下でも動く (末尾の `/` は付けて開く)

Apache (`mod_proxy` と `mod_proxy_http` が必要。`X-Forwarded-For` は自動で付く):

```apache
ProxyPass        /discord_mcp/mcp/   http://127.0.0.1:8011/mcp/
ProxyPassReverse /discord_mcp/mcp/   http://127.0.0.1:8011/mcp/
ProxyPass        /discord_mcp/admin/ http://127.0.0.1:8012/
ProxyPassReverse /discord_mcp/admin/ http://127.0.0.1:8012/
```

nginx:

```nginx
location /discord_mcp/mcp/ {
    proxy_pass http://127.0.0.1:8011/mcp/;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
location /discord_mcp/admin/ {
    proxy_pass http://127.0.0.1:8012/;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

管理画面に出すコネクタの URL は、既定では管理画面の URL から決める (`.../admin/` と同じ階層の `.../mcp/<トークン>`)。
MCP サーバーを別の場所 (別のパスやドメイン) で公開するときは、`DISCORD_MCP_PUBLIC_URL` (例: `https://example.com/discord_mcp/mcp`) を
`/etc/default/discord-mcp-admin` に書いて管理画面を再起動する。`clients.py` で URL を表示するときも、この環境変数を付けると完全な URL が出る。

### 4. ボットを作り、登録する

「[ボットの作成と招待](#ボットの作成と招待)」でボットを作り、管理画面 (`https://example.com/discord_mcp/admin/`) の
**ボットとサーバー** でトークンを登録する。接続テストで intent・入っているサーバー・足りない権限を確かめ、招待 URL からサーバーに招待する。

### 5. AI のコネクタを作り、AI に登録する

管理画面の **AI コネクタ** で AI を追加すると、その AI 専用のコネクタ URL がコピーされる。
claude.ai なら「設定 → コネクタ → カスタムコネクタを追加」で、URL にそれを入れる (認証の設定は不要。URL のトークンが鍵の代わり)。
URL を作り直したら、その AI のコネクタの URL も変える。

### 更新

```bash
cd /opt/discord-mcp && git pull --ff-only
sudo systemctl restart discord-mcp.service discord-mcp-admin.service
```

`requirements.txt` を変えたときは、再起動の前に `.venv/bin/pip install -r requirements.txt` を、
systemd の定義を変えたときは `install` と `daemon-reload` もやり直す。

---

## アカウント (ボット)

アカウント (ボット) ごとに `accounts/<アカウント名>.toml` が 1 つ (Git 管理外。ボットのトークンを含む)。管理画面で作れるが、手で書いてもよい。
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

手で書くときは:

```bash
mkdir -p accounts && chmod 700 accounts
cp account.example.toml accounts/main.toml && chmod 600 accounts/main.toml
vi accounts/main.toml                                # bot_token を書く
```

### ボットの作成と招待

1. https://discord.com/developers/applications で **New Application** (名前がボットの表示名の元になる)
2. 左の **Bot** で
   - **Reset Token** → 出たトークンを `accounts/<アカウント名>.toml` の `bot_token` に書く (トークンは一度しか表示されない)
   - **Privileged Gateway Intents** の **Message Content Intent** をオン (オフだとボット宛てでないメッセージの本文が空になり、本文の検索もできない)
   - **Server Members Intent** をオン (メンバーの一覧に要る。名前での検索だけなら無くても動く)
   - 他人に入れさせないなら **Public Bot** をオフ
3. サーバーに招待する。管理画面の接続テストか、MCP の `discord_list_accounts` で招待 URL (`invite_url`) が出るので、それを開いてサーバーを選ぶ。
   手で作るなら **OAuth2 → URL Generator** で scope `bot`、権限は次のとおり (`permissions=309774896192`):
   View Channels / Send Messages / Send Messages in Threads / Create Public Threads / Embed Links / Attach Files /
   Read Message History / Add Reactions / Use External Emojis / Manage Webhooks
4. 非公開のチャンネルは、そのチャンネルの権限でボット (かボットのロール) に「チャンネルを見る」「メッセージ履歴を読む」を許可する

「ウェブフックの管理」(Manage Webhooks) は、AI の名前で投稿する (下の `post_as = "webhook"`) ために要る。
ボットの投稿だけでよい (すべての AI を `post_as = "bot"` にする) なら外してよい。

---

## 管理画面

`https://example.com/discord_mcp/admin/` でログインして、次のことができる。保存は再起動しなくても MCP サーバーにすぐ効く。

- **AI コネクタ**: AI の追加・編集・削除、コネクタ URL の表示・コピー・作り直し (表示名・アイコン・投稿方法・使えるボット・読み取り専用)
- **ボットとサーバー**: `accounts/<名前>.toml` の作成・編集・削除 (削除は `accounts/.deleted/` へ移す)。トークンは画面に表示せず、空のまま保存すると今のトークンを残す。
  接続テストで、ボットのユーザー、Message Content Intent・Server Members Intent がオンか、Public Bot か、ボットが入っているサーバーと
  そのサーバーで足りない権限 (ロールから計算したサーバー全体のもの) を確かめる。扱うサーバーを選んで絞れる。招待 URL を開く・コピーできる
- **ボットの作り方**: Developer Portal での手順と、招待に含める権限の一覧

- ログイン情報は `/etc/default/discord-mcp-admin` (上の「[2. systemd に登録する](#2-systemd-に登録する)」)。開発時はリポジトリ直下の `idpass.txt` (1 行目がユーザー名、2 行目がパスワード。Git 管理外) でもよい
- Cookie は HttpOnly・SameSite=Strict。service の例では `DISCORD_MCP_ADMIN_SECURE_COOKIE=1` で Secure も付ける (HTTPS で公開する前提)。書き込みの API は JSON しか受け付けない (他サイトからのフォーム送信を防ぐ)
- 同じ接続元からログインに 10 分で 10 回失敗すると、しばらく受け付けない

---

## AI ごとのコネクタ (clients.json)

AI ごとにコネクタの URL を発行する。設定は `clients.json` (Git 管理外・600) にあり、管理画面か `clients.py` (リポジトリ直下で実行する) で管理する。
サーバーは毎回ファイルを確かめるので、追加・変更・作り直しに再起動は要らない。

```bash
export DISCORD_MCP_PUBLIC_URL=https://example.com/discord_mcp/mcp   # 付けると完全な URL を表示する
.venv/bin/python clients.py add claude --name Claude --avatar-url https://example.com/claude.png   # URL が表示される
.venv/bin/python clients.py add chatgpt --name ChatGPT --accounts main
.venv/bin/python clients.py add viewer --post-as bot --read-only
.venv/bin/python clients.py list
.venv/bin/python clients.py set chatgpt --post-as bot        # 変える
.venv/bin/python clients.py url claude                        # URL を表示する
.venv/bin/python clients.py regenerate claude                 # トークンを作り直す (前の URL はすぐ使えなくなる)
.venv/bin/python clients.py remove viewer
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
- 時刻は `DISCORD_MCP_TZ` (既定 Asia/Tokyo) の ISO 8601 で返す。`before` / `after` はメッセージ ID か ISO 8601 (タイムゾーンなしは `DISCORD_MCP_TZ`)
- 本文の `<@id>` `<#id>` `<@&id>` `<:name:id>` `<t:…>` は名前や日時に置き換えて返す
- 送信は既定で `@everyone` / `@here` / ロールのメンションを通知しない (`notify_everyone` / `notify_roles` を true にしたときだけ通知する)
- 検索 (`discord_search_messages`) は Discord 側のインデックスを使う。作っている途中だと「待ってやり直す」というエラーになる
- Discord に予約送信・下書きはないので、そのツールは無い

---

## 環境変数

| キー | 内容 |
| --- | --- |
| `DISCORD_MCP_PORT` | MCP サーバーの待ち受けポート (既定 8011。`127.0.0.1` だけで待ち受ける) |
| `DISCORD_MCP_PUBLIC_URL` | コネクタの URL の前半 (例 `https://example.com/discord_mcp/mcp`)。空なら管理画面の URL から決める |
| `DISCORD_MCP_CLIENTS_FILE` | `clients.json` の場所 (既定はリポジトリ直下) |
| `DISCORD_MCP_ACCOUNTS_DIR` | アカウントの設定のディレクトリ (既定はリポジトリ直下の `accounts/`) |
| `DISCORD_MCP_TZ` | 時刻の表示と、タイムゾーンなしの時刻の解釈に使うタイムゾーン (既定 `Asia/Tokyo`) |
| `DISCORD_MCP_ADMIN_USERNAME` / `DISCORD_MCP_ADMIN_PASSWORD` / `DISCORD_MCP_ADMIN_SECRET` | 管理画面のログイン情報と Cookie の署名の鍵 |
| `DISCORD_MCP_ADMIN_SECURE_COOKIE` | `1` なら管理画面の Cookie に Secure を付ける |
| `DISCORD_API_BASE` | Discord API の場所 (試験で偽のサーバーに向けるときだけ使う) |

---

## 開発

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tests/smoke.py        # 偽の Discord に対する通し試験
.venv/bin/python tests/admin_test.py   # 管理画面の API の試験
```

どちらの試験も実際の Discord には接続しない。手元で実際の Discord に向けて動かすときは、`accounts/` にトークンを置いて:

```bash
.venv/bin/python clients.py add local --name Local
.venv/bin/python mcp_server/server.py                                        # http://127.0.0.1:8011/mcp/<トークン>
.venv/bin/python -m uvicorn --app-dir web api:app --host 127.0.0.1 --port 8012   # 管理画面 http://127.0.0.1:8012/
```

送信系のツールは本当に送られるので注意する。

---

## 安全のために

- コネクタの URL はトークンそのもの。漏れたら管理画面か `python clients.py regenerate <id>` で作り直す (前の URL はすぐ使えなくなる)。
  リバースプロキシのアクセスログには URL ごと残る (MCP サーバー自身のログには、AI の id しか残さない)
- 接続元の AI は入口で URL のトークンから決める。外から付けた `x-discord-mcp-client` ヘッダーは捨てる
- ボットのトークンが漏れたら Developer Portal で Reset Token して `bot_token` を書き換える
- 発言はボットか AI の名前でサーバーの人たちに届く。信用しきれない AI のコネクタは `--read-only` にするか、`--accounts` で読み取り専用のアカウントだけにする
- ボットが入っているが AI に触らせたくないサーバーがあれば、アカウントの `servers` で扱うサーバーを絞る
- 管理画面はボットのトークンとすべてのコネクタを扱える。パスワードは長いものにする
- `accounts/`・`clients.json`・`idpass.txt`・`/etc/default/discord-mcp-admin` は Git 管理外。サーバーの外に出さない
