"""管理画面の API の試験。偽の Discord API を立て、FastAPI の TestClient で呼ぶ。

実行 (リポジトリ直下で):  python tests/admin_test.py
実際の Discord には接続しない。
"""
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time

import uvicorn

ROOT = pathlib.Path(__file__).resolve().parent.parent
DISCORD_PORT = 18101
tmp = pathlib.Path(tempfile.mkdtemp(prefix='discord-mcp-admin-test-'))
os.environ.update({
    'DISCORD_API_BASE': f'http://127.0.0.1:{DISCORD_PORT}/api',
    'DISCORD_MCP_ACCOUNTS_DIR': str(tmp / 'accounts'),
    'DISCORD_MCP_CLIENTS_FILE': str(tmp / 'clients.json'),
    'DISCORD_MCP_PUBLIC_URL': 'https://example.com/discord_mcp/mcp',
    'DISCORD_MCP_ADMIN_USERNAME': 'admin',
    'DISCORD_MCP_ADMIN_PASSWORD': 'pass-word',
    'DISCORD_MCP_ADMIN_SECRET': 'test-secret',
})
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'web'))
sys.path.insert(0, str(ROOT / 'tests'))
import fake_discord as fd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import accounts  # noqa: E402
import api  # noqa: E402
import clients  # noqa: E402

failures = []


def check(name, cond, info=''):
    print(('  ok   ' if cond else '  FAIL ') + name + (f'  {info}' if info and not cond else ''))
    if not cond:
        failures.append(name)


def run():
    c = TestClient(api.app)
    J = {'Content-Type': 'application/json'}

    # ---- 画面とログイン ----
    r = c.get('/')
    check('index served', r.status_code == 200 and 'discord-mcp 管理' in r.text)
    check('api needs login', c.get('/api/clients').status_code == 401)
    r = c.post('/api/login', json={'username': 'admin', 'password': 'wrong'})
    check('wrong password 401', r.status_code == 401)
    for _ in range(9):
        c.post('/api/login', json={'username': 'admin', 'password': 'wrong'})
    r = c.post('/api/login', json={'username': 'admin', 'password': 'pass-word'})
    check('login rate limited after 10 failures', r.status_code == 429, r.status_code)
    api._failures.clear()
    r = c.post('/api/login', json={'username': 'admin', 'password': 'pass-word'})
    check('login ok, cookie set', r.status_code == 200 and api.AUTH_COOKIE in c.cookies, r.text)
    sc = r.headers.get('set-cookie', '').lower()
    check('cookie httponly + samesite strict', 'httponly' in sc and 'samesite=strict' in sc, sc)
    check('session', c.get('/api/session').json()['username'] == 'admin')

    # ---- CSRF: JSON 以外の書き込みは断る ----
    r = c.post('/api/clients', data={'id': 'x', 'name': 'X'})
    check('form post rejected (415)', r.status_code == 415, r.status_code)

    # ---- ボット (アカウント) ----
    r = c.post('/api/accounts', json={'name': 'main', 'label': 'メイン "A" \\ テスト', 'bot_token': fd.TOKEN_A})
    check('account created', r.status_code == 200 and r.json()['bot_token'] == fd.TOKEN_A[:6] + '…' + fd.TOKEN_A[-4:]
          and r.json()['servers'] == [], r.text)
    path = tmp / 'accounts' / 'main.toml'
    cfg = accounts.read_account('main')
    check('toml readable with escaped label', cfg['label'] == 'メイン "A" \\ テスト' and cfg['bot_token'] == fd.TOKEN_A, cfg)
    if os.name != 'nt':
        check('toml mode 600', stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode))
    body = c.get('/api/accounts').text
    check('token never returned in full', fd.TOKEN_A not in body, body)
    r = c.post('/api/accounts', json={'name': 'main', 'bot_token': fd.TOKEN_A})
    check('duplicate account refused', r.status_code == 400 and 'すでにある' in r.json()['detail'], r.text)
    r = c.post('/api/accounts', json={'name': 'Bad Name', 'bot_token': fd.TOKEN_A})
    check('bad account name refused', r.status_code == 400, r.text)
    r = c.post('/api/accounts', json={'name': 'x', 'bot_token': 'xoxb-slack-token'})
    check('non-discord token refused', r.status_code == 400 and 'bot_token' in r.json()['detail'], r.text)
    r = c.post('/api/accounts', json={'name': 'x', 'bot_token': fd.TOKEN_B, 'servers': ['abc']})
    check('bad server id refused', r.status_code == 400 and 'サーバー ID' in r.json()['detail'], r.text)
    c.post('/api/accounts', json={'name': 'sub', 'bot_token': fd.TOKEN_B, 'read_only': True})

    # 接続テスト: intent・サーバー・権限
    t = c.post('/api/accounts/test', json={'name': 'main'}).json()
    check('test: bot + intents', t['ok'] and t['bot']['username'] == 'helper-a' and t['message_content_intent']
          and t['server_members_intent'] and t['application']['public'] is False, t)
    check('test: invite url', t['invite_url'].endswith(f'permissions={api.INVITE_PERMISSIONS}')
          and 'client_id=900000000000000001' in t['invite_url'], t['invite_url'])
    servers = {s['name']: s for s in t['servers']}
    check('test: servers listed', set(servers) == {'ゲーム仲間', 'Work Lab'}, t['servers'])
    check('test: missing webhook permission', servers['ゲーム仲間']['missing'] == ['ウェブフックの管理 (AI の名前で投稿するため)'],
          servers['ゲーム仲間'])
    check('test: administrator', servers['Work Lab'].get('administrator') and servers['Work Lab']['missing'] == [], servers['Work Lab'])
    t = c.post('/api/accounts/test', json={'name': 'sub'}).json()
    check('test: intents off + public + all missing', not t['message_content_intent'] and not t['server_members_intent']
          and t['application']['public'] and len(t['servers'][0]['missing']) == len(api.PERMISSIONS), t)
    t = c.post('/api/accounts/test', json={'bot_token': 'BADTOKENXXXXXXXXXXX.abcdef.xxxxxxxxxxxxxxxxxxxxxx'}).json()
    check('test: invalid token', t['ok'] is False and 'Reset Token' in t['error'], t)
    t = c.post('/api/accounts/test', json={'bot_token': 'not-a-token'}).json()
    check('test: malformed token', t['ok'] is False and '形が違う' in t['error'], t)
    r = c.post('/api/accounts/test', json={})
    check('test: needs token', r.status_code == 400, r.text)

    # 編集: トークンは空なら残す、servers は渡したときだけ変える
    c.put('/api/accounts/main', json={'label': '新しい説明', 'read_only': True, 'servers': [fd.G2]})
    cfg = accounts.read_account('main')
    check('update keeps token, sets servers', cfg['bot_token'] == fd.TOKEN_A and cfg['label'] == '新しい説明'
          and cfg['read_only'] and cfg['servers'] == frozenset({fd.G2}), cfg)
    c.put('/api/accounts/main', json={'read_only': False})
    cfg = accounts.read_account('main')
    check('update without servers keeps them', cfg['servers'] == frozenset({fd.G2}) and not cfg['read_only'], cfg)
    c.put('/api/accounts/main', json={'servers': []})
    check('empty servers = all', accounts.read_account('main')['servers'] == frozenset())
    r = c.put('/api/accounts/nope', json={'label': 'x'})
    check('update missing account 400', r.status_code == 400, r.text)

    # ---- AI コネクタ ----
    r = c.post('/api/clients', json={'id': 'claude', 'name': 'Claude', 'avatar_url': 'https://example.com/c.png', 'accounts': ['main']})
    cl = r.json()
    check('client created with url', r.status_code == 200 and cl['url'].startswith('https://example.com/discord_mcp/mcp/')
          and cl['post_as'] == 'webhook' and cl['accounts'] == ['main'] and cl['avatar_url'] == 'https://example.com/c.png', r.text)
    saved_base, clients.PUBLIC_BASE = clients.PUBLIC_BASE, ''
    check('relative url without public url', clients.connector_url(cl) == '../mcp/' + cl['token'])
    clients.PUBLIC_BASE = saved_base
    found = clients.find_by_token(cl['token'])
    check('MCP side sees new client immediately', found is not None and found['id'] == 'claude', found)
    r = c.post('/api/clients', json={'id': 'claude', 'name': 'X'})
    check('duplicate client refused', r.status_code == 400, r.text)
    r = c.post('/api/clients', json={'id': 'gpt', 'name': 'Discord Helper'})
    check('name with "discord" refused', r.status_code == 400 and 'discord' in r.json()['detail'], r.text)
    for bad in ('Claude @home', 'AI #1', 'Bot: v2', 'everyone', 'CLYDE'):
        r = c.post('/api/clients', json={'id': 'gpt', 'name': bad})
        check(f'name {bad!r} refused', r.status_code == 400 and '表示名' in r.json()['detail'], r.text)
    r = c.post('/api/clients', json={'id': 'gpt', 'name': 'GPT', 'avatar_url': 'http://insecure'})
    check('non-https avatar refused', r.status_code == 400 and 'https' in r.json()['detail'], r.text)
    r = c.post('/api/clients', json={'id': 'gpt', 'name': 'GPT', 'post_as': 'user'})
    check('bad post_as refused', r.status_code == 400, r.text)
    r = c.put('/api/clients/claude', json={'post_as': 'bot', 'avatar_url': '', 'read_only': True, 'accounts': []})
    cl2 = r.json()
    check('client updated', cl2['post_as'] == 'bot' and cl2['avatar_url'] is None and cl2['read_only']
          and cl2['accounts'] == [] and cl2['token'] == cl['token'], cl2)
    r = c.post('/api/clients/claude/regenerate', json={})
    check('regenerate changes token', r.json()['token'] != cl['token'] and clients.find_by_token(cl['token']) is None, r.text)
    r = c.put('/api/clients/nope', json={'name': 'x'})
    check('update missing client 404', r.status_code == 404, r.text)
    check('list clients', [x['id'] for x in c.get('/api/clients').json()['clients']] == ['claude'])
    r = c.request('DELETE', '/api/clients/claude', headers=J, content='{}')
    check('delete client', r.status_code == 200 and clients.load_clients() == [], r.text)

    # ---- アカウントの削除は .deleted へ移す ----
    r = c.request('DELETE', '/api/accounts/sub', headers=J, content='{}')
    check('delete account moves file', r.status_code == 200 and r.json()['moved_to'].startswith('.deleted')
          and not (tmp / 'accounts' / 'sub.toml').exists() and accounts.list_accounts() == ['main'], r.text)

    s = c.get('/api/setup').json()
    check('setup permissions', s['permissions_value'] == api.INVITE_PERMISSIONS and len(s['permissions']) == 10, s)

    c.post('/api/logout', headers=J, content='{}')
    c.cookies.clear()
    check('logged out', c.get('/api/clients').status_code == 401)


def main():
    fd.BASE['url'] = f'http://127.0.0.1:{DISCORD_PORT}'
    server = uvicorn.Server(uvicorn.Config(fd.app, host='127.0.0.1', port=DISCORD_PORT, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        run()
    finally:
        server.should_exit = True
        thread.join(5)
    print(f'\n{len(failures)} failures' if failures else '\nall passed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
