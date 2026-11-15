"""MCP の接続元 (AI ごとのコネクタ) の設定と、コネクタ URL のトークンの管理。

AI ごとに 1 つのコネクタ URL (/mcp/<トークン>) を発行し、URL のトークンでどの AI かを見分ける。
設定は clients.json (環境変数 DISCORD_MCP_CLIENTS_FILE、既定はリポジトリ直下。Git 管理外・600) に持つ。
サーバーはファイルの更新を毎回確かめるので、追加・作り直しに再起動は要らない。
書き込みの関数 (create_client など) は、このファイルのコマンドと管理画面 (web/api.py) の両方が使う。

AI ごとの設定:
  id          英小文字・数字・ハイフン・アンダースコアで 32 文字まで (ログとツールの結果に出る)
  name        webhook で投稿するときの表示名 (例 "Claude"。80 文字まで。"discord" "clyde" "@" "#" ":" "```" を含む名前と、
              "everyone" "here" は Discord が断る)
  avatar_url  webhook で投稿するときのアイコン画像の URL
  post_as     "webhook" (既定。チャンネルの webhook で、name・アイコンを付けて投稿する) か "bot" (ボット自身の名前で投稿する)
  accounts    使ってよいアカウント名の一覧 (省略か空ならすべて)
  read_only   true なら読み取りだけ (送信・変更系のツールを断る)

使い方 (リポジトリ直下で):
  python clients.py list
  python clients.py add <id> --name Claude [--avatar-url https://...] [--post-as bot] [--accounts a,b] [--read-only]
  python clients.py set <id> [--name ...] [--avatar-url ...] [--post-as webhook|bot] [--accounts ...] [--read-only|--writable]
  python clients.py url <id>          コネクタの URL を表示する
  python clients.py regenerate <id>   トークンを作り直す (前の URL はすぐ使えなくなる)
  python clients.py remove <id>
"""
from pathlib import Path
import argparse
import datetime
import hmac
import json
import os
import re
import secrets
import sys
import threading

CLIENTS_FILE = Path(os.environ.get('DISCORD_MCP_CLIENTS_FILE', Path(__file__).resolve().parent / 'clients.json'))
PUBLIC_BASE = os.environ.get('DISCORD_MCP_PUBLIC_URL', 'https://swirhen.tv/discord_mcp/mcp').rstrip('/')
ID_RE = re.compile(r'^[a-z0-9_-]{1,32}$')
TOKEN_RE = re.compile(r'^[0-9a-f]{64}$')
POST_AS = ('webhook', 'bot')
# Discord が webhook の名前・投稿ごとの表示名として断るもの (大文字小文字は区別しない)
BAD_SUBSTRINGS = ('discord', 'clyde', '@', '#', ':', '```')
BAD_EXACT = ('everyone', 'here')


def name_problem(name: str) -> str | None:
    """webhook の表示名として Discord に断られる理由 (問題なければ None)"""
    name = (name or '').strip()
    if not name or len(name) > 80:
        return '表示名は 1〜80 文字'
    lowered = name.lower()
    found = [s for s in BAD_SUBSTRINGS if s in lowered]
    if found:
        return f'表示名に {"・".join(repr(s) for s in found)} は使えない (Discord が webhook の名前として断る)'
    if lowered in BAD_EXACT:
        return f'表示名を "{name}" にはできない (Discord が断る)'
    return None


def safe_webhook_name(name: str) -> str:
    """古い設定などで使えない文字が入っていても投稿が失敗しないよう、使えない部分を取り除く"""
    out = name or ''
    for s in BAD_SUBSTRINGS:
        out = re.sub(re.escape(s), '', out, flags=re.IGNORECASE)
    out = re.sub(r'\s+', ' ', out).strip()[:80]
    return out if out and out.lower() not in BAD_EXACT else 'AI'

_cache: dict = {'key': None, 'clients': []}


def _normalize(raw: dict) -> dict | None:
    cid = str(raw.get('id', ''))
    token = str(raw.get('token', ''))
    if not ID_RE.fullmatch(cid) or not TOKEN_RE.fullmatch(token):
        return None
    post_as = raw.get('post_as', 'webhook')
    return {
        'id': cid,
        'token': token,
        'name': str(raw.get('name') or cid)[:80],
        'avatar_url': raw.get('avatar_url') or None,
        'post_as': post_as if post_as in POST_AS else 'webhook',
        'accounts': [str(a) for a in raw.get('accounts') or []],
        'read_only': bool(raw.get('read_only', False)),
        'created_at': raw.get('created_at'),
    }


def load_clients() -> list[dict]:
    """設定されている AI の一覧 (壊れた項目は除く)。ファイルが変わったときだけ読み直す"""
    try:
        st = CLIENTS_FILE.stat()
    except FileNotFoundError:
        return []
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    if _cache['key'] != key:
        try:
            data = json.loads(CLIENTS_FILE.read_text(encoding='utf-8'))
            raw = data.get('clients', []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            raw = []
        _cache.update(key=key, clients=[c for c in (_normalize(r) for r in raw if isinstance(r, dict)) if c])
    return _cache['clients']


def find_by_token(token: str) -> dict | None:
    """URL のトークンに対応する AI を返す (なければ None)。比較は時間の差が出ない方法で全件行う"""
    found = None
    for c in load_clients():
        if hmac.compare_digest(c['token'], token):
            found = c
    return found


def find_by_id(cid: str) -> dict | None:
    return next((c for c in load_clients() if c['id'] == cid), None)


def connector_url(client: dict) -> str:
    return f'{PUBLIC_BASE}/{client["token"]}'


# ---- 書き込み (CLI と管理画面から使う。値が正しくなければ ValueError、ない id は KeyError) ----
_write_lock = threading.Lock()
OPTION_KEYS = ('name', 'avatar_url', 'post_as', 'accounts', 'read_only')


def _read_raw() -> list[dict]:
    try:
        data = json.loads(CLIENTS_FILE.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    return list(data.get('clients', []))


def _write_raw(clients: list[dict]) -> None:
    temporary = CLIENTS_FILE.with_name(CLIENTS_FILE.name + '.tmp')
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as f:
        json.dump({'clients': clients}, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(temporary, CLIENTS_FILE)


def _apply_options(entry: dict, options: dict) -> None:
    """options の None でない項目を entry に書く (検証つき)"""
    unknown = set(options) - set(OPTION_KEYS)
    if unknown:
        raise ValueError(f'知らない項目: {", ".join(sorted(unknown))}')
    if options.get('name') is not None:
        name = str(options['name']).strip()
        problem = name_problem(name)
        if problem:
            raise ValueError(problem)
        entry['name'] = name
    if options.get('avatar_url') is not None:
        url = str(options['avatar_url']).strip()
        if url and not re.fullmatch(r'https://\S+', url):
            raise ValueError('アイコンの URL は https:// で始める')
        entry['avatar_url'] = url or None
    if options.get('post_as') is not None:
        if options['post_as'] not in POST_AS:
            raise ValueError('投稿方法は webhook か bot')
        entry['post_as'] = options['post_as']
    if options.get('accounts') is not None:
        accounts = options['accounts']
        if isinstance(accounts, str):
            accounts = accounts.split(',')
        entry['accounts'] = [str(a).strip() for a in accounts if str(a).strip()]
    if options.get('read_only') is not None:
        entry['read_only'] = bool(options['read_only'])


def client_view(c: dict) -> dict:
    """管理画面・CLI に見せる形 (URL つき)"""
    out = _normalize(c) or dict(c)
    out['url'] = connector_url(out)
    return out


def create_client(cid: str, **options) -> dict:
    if not ID_RE.fullmatch(cid or ''):
        raise ValueError('id は英小文字・数字・ハイフン・アンダースコアで 32 文字まで')
    with _write_lock:
        raw = _read_raw()
        if any(c.get('id') == cid for c in raw):
            raise ValueError(f'{cid} はすでにある')
        entry = {'id': cid, 'token': secrets.token_hex(32), 'name': cid, 'post_as': 'webhook',
                 'created_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
        _apply_options(entry, options)
        raw.append(entry)
        _write_raw(raw)
    return client_view(entry)


def _change(cid: str, change) -> dict:
    with _write_lock:
        raw = _read_raw()
        entry = next((c for c in raw if c.get('id') == cid), None)
        if entry is None:
            raise KeyError(cid)
        change(entry)
        _write_raw(raw)
    return client_view(entry)


def update_client(cid: str, **options) -> dict:
    return _change(cid, lambda entry: _apply_options(entry, options))


def regenerate_client(cid: str) -> dict:
    """トークンを作り直す。前の URL はすぐ使えなくなる"""
    return _change(cid, lambda entry: entry.update(token=secrets.token_hex(32)))


def remove_client(cid: str) -> None:
    with _write_lock:
        raw = _read_raw()
        rest = [c for c in raw if c.get('id') != cid]
        if len(rest) == len(raw):
            raise KeyError(cid)
        _write_raw(rest)


# ---- CLI ----
def _describe(c: dict) -> str:
    flags = [c['post_as']]
    if c.get('read_only'):
        flags.append('read_only')
    accounts = ','.join(c.get('accounts') or []) or 'すべて'
    avatar = 'あり' if c.get('avatar_url') else '-'
    return f'{c["id"]:<16} 表示名={c["name"]}  アイコン={avatar}  {"/".join(flags)}  アカウント={accounts}'


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='discord-mcp の接続元 (AI ごとのコネクタ) の管理')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='一覧')
    for name in ('add', 'set'):
        p = sub.add_parser(name)
        p.add_argument('id')
        p.add_argument('--name')
        p.add_argument('--avatar-url')
        p.add_argument('--post-as', choices=POST_AS)
        p.add_argument('--accounts', help='使ってよいアカウント名をカンマ区切りで (空文字ですべて)')
        p.add_argument('--read-only', dest='read_only', action='store_true', default=None)
        p.add_argument('--writable', dest='read_only', action='store_false')
    for name in ('url', 'regenerate', 'remove'):
        sub.add_parser(name).add_argument('id')
    args = parser.parse_args(argv)

    if args.command == 'list':
        clients = load_clients()
        if not clients:
            print('AI はまだ登録されていない (python clients.py add <id> --name ...)')
        for c in clients:
            print(_describe(c))
        return
    options = {k: getattr(args, k, None) for k in OPTION_KEYS}
    try:
        if args.command == 'add':
            c = create_client(args.id, **options)
            print(_describe(c))
            print(f'コネクタの URL: {c["url"]}')
        elif args.command == 'set':
            print(_describe(update_client(args.id, **options)))
        elif args.command == 'url':
            c = find_by_id(args.id)
            if c is None:
                raise KeyError(args.id)
            print(connector_url(c))
        elif args.command == 'regenerate':
            print(f'トークンを作り直した。新しいコネクタの URL: {regenerate_client(args.id)["url"]}')
        elif args.command == 'remove':
            remove_client(args.id)
            print(f'{args.id} を消した (その URL はすぐ使えなくなる)')
    except KeyError:
        sys.exit(f'{args.id} は登録されていない (python clients.py list)')
    except ValueError as e:
        sys.exit(str(e))


if __name__ == '__main__':
    main()
