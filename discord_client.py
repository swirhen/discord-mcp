"""Discord REST API (v10) の薄いクライアント (httpx)。

discord.py は使わない (Gateway に常時接続せず、ツールが呼ばれたときだけ REST を呼ぶ。
同じボットが別の場所で常駐していてもぶつからない)。
レート制限 (HTTP 429) は retry_after に従って数回まで待って再試行する。

環境変数
  DISCORD_API_BASE  Discord API の URL (省略時は https://discord.com/api/v10。テストで偽のサーバーに向けるため)
"""
import asyncio
import json
import logging
import os
from typing import Any
from urllib.parse import quote

import httpx

API_BASE = os.environ.get('DISCORD_API_BASE', 'https://discord.com/api/v10').rstrip('/')
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
MAX_RETRIES = 3
MAX_RETRY_WAIT = 30.0
# Discord は User-Agent を "DiscordBot ($url, $versionNumber)" の形にするよう求めている
USER_AGENT = 'DiscordBot (discord-mcp, 1.0)'

log = logging.getLogger('discord-mcp.api')


class DiscordError(Exception):
    """Discord API がエラーを返したか、呼び出し自体に失敗した。
    status は HTTP ステータス (接続できないときは 0)、code は Discord の JSON エラーコード (ないときは 0)"""

    def __init__(self, status: int, code: int, message: str, response: Any = None):
        self.status = status
        self.code = code
        self.message = message
        self.response = response
        super().__init__(f'HTTP {status} code {code}: {message}')


class Pending(Exception):
    """HTTP 202 (検索のインデックスを作っている途中など)。retry_after 秒ほど後にやり直す"""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f'retry after {retry_after}s')


_clients: dict[int, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def http() -> httpx.AsyncClient:
    """イベントループごとの HTTP クライアント (接続はループをまたいで使えないため。管理画面の試験などで別のループから呼ばれる)"""
    loop = asyncio.get_running_loop()
    for key, (other, _) in list(_clients.items()):
        if other.is_closed():
            del _clients[key]
    entry = _clients.get(id(loop))
    if entry is None or entry[0] is not loop or entry[1].is_closed:
        entry = (loop, httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={'User-Agent': USER_AGENT}))
        _clients[id(loop)] = entry
    return entry[1]


def _query(params: dict[str, Any] | None) -> list[tuple[str, str]] | None:
    """None を除き、bool は true/false、配列は同じキーを繰り返す形にする"""
    if not params:
        return None
    out = []
    for key, value in params.items():
        if value is None:
            continue
        for v in value if isinstance(value, (list, tuple)) else [value]:
            out.append((key, ('true' if v else 'false') if isinstance(v, bool) else str(v)))
    return out


def _error_detail(body: Any) -> str:
    """{'message': ..., 'errors': {...}} の errors を「場所: 内容」の形にまとめる"""
    if not isinstance(body, dict):
        return ''
    found = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for err in node.get('_errors', []) or []:
                found.append(f'{path or "body"}: {err.get("message", err.get("code", ""))}')
            for k, v in node.items():
                if k != '_errors':
                    walk(v, f'{path}.{k}' if path else k)

    walk(body.get('errors'), '')
    return '; '.join(found[:5])


async def request(token: str, method: str, path: str, *, params: dict | None = None, body: Any = None,
                  files: list[tuple[str, bytes, str]] | None = None, reason: str | None = None) -> Any:
    """Discord API を呼んで JSON (204 なら None) を返す。失敗は DiscordError、202 は Pending。
    files を渡すと multipart で送り、body は payload_json にする。
    token が空なら認証ヘッダーを付けない (webhook の実行は URL のトークンで認証する)"""
    headers = {'Authorization': f'Bot {token}'} if token else {}
    if reason:  # ヘッダーは ASCII だけなので、Discord の決まりどおり URL エンコードする
        headers['X-Audit-Log-Reason'] = quote(reason, safe='')
    url = f'{API_BASE}{path}'
    kwargs: dict[str, Any] = {'params': _query(params), 'headers': headers}
    if files:
        kwargs['data'] = {'payload_json': json.dumps(body or {}, ensure_ascii=False)}
        kwargs['files'] = [(f'files[{i}]', (name, data, ctype)) for i, (name, data, ctype) in enumerate(files)]
    elif body is not None:
        kwargs['json'] = body
    for attempt in range(MAX_RETRIES + 1):
        try:
            res = await http().request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise DiscordError(0, 0, f'Discord API に接続できない ({type(e).__name__}: {e})') from e
        if res.status_code == 429:
            wait = _retry_after(res)
            if attempt >= MAX_RETRIES:
                raise DiscordError(429, 0, f'Discord API のレート制限 ({method} {path})。{wait:.0f} 秒ほど待ってからやり直す')
            log.warning('%s %s: ratelimited, retry in %.1fs', method, path, wait)
            await asyncio.sleep(wait)
            continue
        if res.status_code == 204:
            return None
        try:
            data = res.json()
        except ValueError:
            data = None
        if res.status_code == 202:
            raise Pending(_retry_after(res, data))
        if res.status_code >= 400:
            message = data.get('message', '') if isinstance(data, dict) else ''
            detail = _error_detail(data)
            code = data.get('code', 0) if isinstance(data, dict) else 0
            raise DiscordError(res.status_code, code, '; '.join(m for m in (message, detail) if m) or res.reason_phrase, data)
        if data is None:
            raise DiscordError(res.status_code, 0, f'Discord API の応答が JSON ではない ({method} {path})')
        return data
    raise DiscordError(429, 0, 'ratelimited')  # ここには来ない


def _retry_after(res: httpx.Response, data: Any = None) -> float:
    if data is None:
        try:
            data = res.json()
        except ValueError:
            data = None
    value: Any = data.get('retry_after') if isinstance(data, dict) else None
    if value is None:
        value = res.headers.get('Retry-After', '2')
    try:
        return max(0.5, min(MAX_RETRY_WAIT, float(value)))
    except (TypeError, ValueError):
        return 2.0


async def download(url: str, max_bytes: int) -> tuple[bytes, str]:
    """添付ファイルの URL (署名つきの CDN の URL なので認証は要らない) を取得する。(本体, Content-Type) を返す"""
    try:
        async with http().stream('GET', url) as res:
            if res.status_code != 200:
                raise DiscordError(res.status_code, 0, f'ファイルの取得が HTTP {res.status_code} で失敗した')
            chunks = []
            size = 0
            async for chunk in res.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise DiscordError(413, 0, f'ファイルが {max_bytes // (1024 * 1024)} MB を超えるので取得しない')
                chunks.append(chunk)
            return b''.join(chunks), res.headers.get('content-type', '')
    except httpx.HTTPError as e:
        raise DiscordError(0, 0, f'ファイルを取得できない ({type(e).__name__}: {e})') from e
