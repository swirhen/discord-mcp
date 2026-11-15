"""discord-mcp の管理画面 (FastAPI)。AI ごとのコネクタ (clients.json) と Discord のボット (accounts/*.toml) を編集する。

画面は同じディレクトリの index.html を / で配る (ビルドは要らない)。API はすべて /api/ の下で、相対パスで呼ぶので、
Apache の /discord_mcp/admin/ の下でも、ローカルの http://127.0.0.1:8012/ でも動く。

ログイン情報は環境変数 DISCORD_MCP_ADMIN_USERNAME / DISCORD_MCP_ADMIN_PASSWORD / DISCORD_MCP_ADMIN_SECRET
(本番は /etc/default/discord-mcp-admin)。ローカルではリポジトリ直下の idpass.txt (1 行目がユーザー名、2 行目がパスワード)。
パスワードか SECRET がないと、誰もログインできない。

MCP サーバーはどちらのファイルも毎回確かめるので、ここで保存すると再起動しなくてもすぐ効く。

起動 (リポジトリ直下で):
  python -m uvicorn --app-dir web api:app --host 127.0.0.1 --port 8012
"""
from pathlib import Path
import asyncio
import hashlib
import hmac
import os
import sys
import time

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

import accounts  # noqa: E402
import clients  # noqa: E402
from bot import ADMINISTRATOR, INVITE_PERMISSIONS, PERMISSIONS, invite_url  # noqa: E402
from discord_client import DiscordError, request  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent
IDPASS_FILE = REPO_DIR / 'idpass.txt'
AUTH_COOKIE = 'discord_mcp_admin_session'
SESSION_TTL = 60 * 60 * 24 * 30
LOGIN_WINDOW = 600
LOGIN_MAX_FAILURES = 10

# アプリケーションの flags (Developer Portal の Privileged Gateway Intents)
FLAG_MEMBERS = (1 << 14) | (1 << 15)
FLAG_MESSAGE_CONTENT = (1 << 18) | (1 << 19)


def load_local_credentials() -> tuple[str, str]:
    try:
        lines = IDPASS_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        return 'admin', ''
    return (lines[0].strip() if lines else 'admin', lines[1].strip() if len(lines) > 1 else '')


LOCAL_USERNAME, LOCAL_PASSWORD = load_local_credentials()
USERNAME = os.getenv('DISCORD_MCP_ADMIN_USERNAME', LOCAL_USERNAME)
PASSWORD = os.getenv('DISCORD_MCP_ADMIN_PASSWORD', LOCAL_PASSWORD)
SECRET = os.getenv('DISCORD_MCP_ADMIN_SECRET', 'local-debug-secret' if IDPASS_FILE.is_file() else '')
SECURE_COOKIE = os.getenv('DISCORD_MCP_ADMIN_SECURE_COOKIE', '') in ('1', 'true', 'yes')

app = FastAPI(title='discord-mcp admin', docs_url=None, redoc_url=None, openapi_url=None)
_failures: dict[str, list[float]] = {}


# ---------------- 認証・共通 ----------------
def make_session_token(issued_at: int) -> str:
    token = hmac.new(SECRET.encode(), f'{issued_at}:{USERNAME}'.encode(), hashlib.sha256).hexdigest()
    return f'{issued_at}:{token}'


def verify_session_token(value: str | None) -> bool:
    if not PASSWORD or not SECRET or not value:
        return False
    try:
        issued_at = int(value.split(':', 1)[0])
    except ValueError:
        return False
    if time.time() - issued_at > SESSION_TTL:
        return False
    return hmac.compare_digest(value, make_session_token(issued_at))


def require_auth(authorization: str | None = Header(default=None),
                 session: str | None = Cookie(default=None, alias=AUTH_COOKIE)):
    if authorization and authorization.startswith('Bearer ') and verify_session_token(authorization[7:].strip()):
        return
    if verify_session_token(session):
        return
    raise HTTPException(401, 'ログインが必要です')


async def json_body(request: Request) -> dict:
    # 他サイトからのフォーム送信 (CSRF) を防ぐため、JSON 以外は受け付けない
    if not request.headers.get('content-type', '').startswith('application/json'):
        raise HTTPException(415, 'Content-Type が application/json ではありません')
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, 'JSON を読めません') from None
    if not isinstance(data, dict):
        raise HTTPException(400, 'JSON のオブジェクトを送ってください')
    return data


def client_ip(request: Request) -> str:
    forwarded = request.headers.get('x-forwarded-for', '')
    return forwarded.split(',')[0].strip() or (request.client.host if request.client else '-')


@app.exception_handler(ValueError)
async def value_error_handler(request, exc):
    return JSONResponse(status_code=400, content={'detail': str(exc)})


@app.exception_handler(KeyError)
async def key_error_handler(request, exc):
    return JSONResponse(status_code=404, content={'detail': f'{exc.args[0] if exc.args else ""} が見つかりません'})


AUTH = [Depends(require_auth)]


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get('/')
def index():
    return FileResponse(WEB_DIR / 'index.html', headers={'Cache-Control': 'no-store'})


@app.get('/api/health')
def health():
    return {'status': 'ok'}


@app.get('/api/session', dependencies=AUTH)
def session():
    return {'authenticated': True, 'username': USERNAME}


@app.post('/api/login')
def login(payload: LoginRequest, request: Request, response: Response):
    ip = client_ip(request)
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < LOGIN_WINDOW]
    if len(recent) >= LOGIN_MAX_FAILURES:
        raise HTTPException(429, '失敗が続いたので、しばらく待ってからやり直してください')
    if not PASSWORD or not SECRET or not hmac.compare_digest(payload.username, USERNAME) \
            or not hmac.compare_digest(payload.password, PASSWORD):
        _failures[ip] = recent + [now]
        raise HTTPException(401, 'ユーザー名またはパスワードが違います')
    _failures.pop(ip, None)
    value = make_session_token(int(now))
    response.set_cookie(AUTH_COOKIE, value, httponly=True, samesite='strict', secure=SECURE_COOKIE, max_age=SESSION_TTL)
    return {'authenticated': True}


@app.post('/api/logout')
def logout(response: Response):
    response.delete_cookie(AUTH_COOKIE)
    return {'authenticated': False}


# ---------------- AI ごとのコネクタ ----------------
def client_options(data: dict) -> dict:
    return {k: data[k] for k in clients.OPTION_KEYS if k in data}


@app.get('/api/clients', dependencies=AUTH)
def list_clients():
    return {'clients': [clients.client_view(c) for c in clients.load_clients()]}


@app.post('/api/clients', dependencies=AUTH)
def add_client(data: dict = Depends(json_body)):
    return clients.create_client(str(data.get('id', '')).strip(), **client_options(data))


@app.put('/api/clients/{cid}', dependencies=AUTH)
def put_client(cid: str, data: dict = Depends(json_body)):
    return clients.update_client(cid, **client_options(data))


@app.post('/api/clients/{cid}/regenerate', dependencies=AUTH)
def regenerate(cid: str, _: dict = Depends(json_body)):
    return clients.regenerate_client(cid)


@app.delete('/api/clients/{cid}', dependencies=AUTH)
def delete_client(cid: str, _: dict = Depends(json_body)):
    clients.remove_client(cid)
    return {'ok': True}


# ---------------- Discord のボット (アカウント) ----------------
def mask(token: str | None) -> str | None:
    """トークンは画面に返さない。先頭 6 文字と末尾 4 文字だけ見せる"""
    if not token:
        return None
    return f'{token[:6]}…{token[-4:]}'


def account_view(name: str) -> dict:
    try:
        c = accounts.read_account(name)
    except accounts.AccountError as e:
        return {'name': name, 'error': str(e)}
    return {'name': name, 'label': c['label'], 'read_only': c['read_only'],
            'servers': sorted(c['servers']), 'bot_token': mask(c['bot_token'])}


def account_error(e: accounts.AccountError):
    return HTTPException(400, str(e))


@app.get('/api/accounts', dependencies=AUTH)
def list_accounts():
    return {'accounts': [account_view(n) for n in accounts.list_accounts()]}


@app.post('/api/accounts', dependencies=AUTH)
def add_account(data: dict = Depends(json_body)):
    name = str(data.get('name', '')).strip()
    try:
        accounts.write_account(name, bot_token=data.get('bot_token', ''), label=data.get('label', ''),
                               read_only=bool(data.get('read_only')), servers=data.get('servers') or [], create=True)
    except accounts.AccountError as e:
        raise account_error(e) from e
    return account_view(name)


@app.put('/api/accounts/{name}', dependencies=AUTH)
def put_account(name: str, data: dict = Depends(json_body)):
    """bot_token は空なら今のまま。servers は渡したときだけ変える (空の配列ですべてのサーバー)"""
    try:
        current = accounts.read_account(name)
        servers = data['servers'] if 'servers' in data else sorted(current['servers'])
        accounts.write_account(name, bot_token=data.get('bot_token') or current['bot_token'],
                               label=data.get('label', current['label']),
                               read_only=bool(data.get('read_only', current['read_only'])), servers=servers)
    except accounts.AccountError as e:
        raise account_error(e) from e
    return account_view(name)


@app.delete('/api/accounts/{name}', dependencies=AUTH)
def remove_account(name: str, _: dict = Depends(json_body)):
    try:
        dest = accounts.delete_account(name)
    except accounts.AccountError as e:
        raise account_error(e) from e
    return {'ok': True, 'moved_to': str(dest.relative_to(accounts.ACCOUNTS_DIR))}


def describe_error(e: DiscordError) -> str:
    if e.status == 401:
        return 'トークンが無効 (Developer Portal の Bot ページで Reset Token して入れ直す)'
    if e.status == 0:
        return f'Discord に接続できない ({e.message})'
    return f'HTTP {e.status}: {e.message}'


async def server_check(token: str, bot_id: str, guild: dict) -> dict:
    """そのサーバーでのボットの権限 (ロールから計算したサーバー全体のもの) と、足りない権限"""
    out = {'id': guild['id'], 'name': guild.get('name'), 'members': guild.get('approximate_member_count')}
    try:
        member, roles = await asyncio.gather(request(token, 'GET', f'/guilds/{guild["id"]}/members/{bot_id}'),
                                             request(token, 'GET', f'/guilds/{guild["id"]}/roles'))
    except DiscordError as e:
        out['error'] = describe_error(e)
        return out
    granted = 0
    mine = set(member.get('roles', [])) | {guild['id']}  # @everyone のロール ID はサーバー ID と同じ
    for role in roles:
        if role['id'] in mine:
            granted |= int(role.get('permissions') or 0)
    if granted & ADMINISTRATOR:
        out['administrator'] = True
        out['missing'] = []
    else:
        out['missing'] = [label for bit, label in PERMISSIONS if not granted & (1 << bit)]
    return out


async def check_bot(token: str) -> dict:
    try:
        me, app_info = await asyncio.gather(request(token, 'GET', '/users/@me'), request(token, 'GET', '/applications/@me'))
        guilds = await request(token, 'GET', '/users/@me/guilds', params={'limit': 200, 'with_counts': True})
    except DiscordError as e:
        return {'ok': False, 'error': describe_error(e)}
    flags = int(app_info.get('flags') or 0)
    servers = await asyncio.gather(*(server_check(token, me['id'], g) for g in guilds))
    return {
        'ok': True,
        'bot': {'id': me['id'], 'username': me.get('username')},
        'application': {'id': app_info['id'], 'name': app_info.get('name'), 'public': bool(app_info.get('bot_public'))},
        'message_content_intent': bool(flags & FLAG_MESSAGE_CONTENT),
        'server_members_intent': bool(flags & FLAG_MEMBERS),
        'invite_url': invite_url(app_info['id']),
        'servers': list(servers),
    }


@app.post('/api/accounts/test', dependencies=AUTH)
async def test_bot(data: dict = Depends(json_body)):
    """保存前のトークン、または保存済みのアカウント (name) のトークンで、ボット・intent・入っているサーバーと権限を確かめる。
    入力したトークンが優先"""
    token = (data.get('bot_token') or '').strip()
    if not token and data.get('name'):
        try:
            token = accounts.read_account(str(data['name']))['bot_token']
        except accounts.AccountError as e:
            raise account_error(e) from e
    if not token:
        raise HTTPException(400, 'bot_token を入れてください')
    if not accounts.TOKEN_RE.fullmatch(token):
        return {'ok': False, 'error': 'トークンの形が違う (Developer Portal の Bot ページの Token。点で区切った 3 つの部分)'}
    return await check_bot(token)


@app.get('/api/setup', dependencies=AUTH)
def setup():
    return {'permissions': [label for _, label in PERMISSIONS], 'permissions_value': INVITE_PERMISSIONS}
