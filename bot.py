"""アカウント (ボット) ごとの状態と、Discord の応答の整形。

- 自分 (ボット) のユーザー・アプリケーション、入っているサーバー、サーバーごとのチャンネル・ロールをしばらく覚えておく
- accounts/<名前>.toml の servers で、扱ってよいサーバーを絞る (それ以外のサーバーのチャンネルは断る)
- メッセージ本文の <@id> <#id> <@&id> <:name:id> <t:unix> を名前や日時に置き換える

環境変数
  DISCORD_MCP_TZ  時刻の表示に使うタイムゾーン (省略時は Asia/Tokyo)
"""
from typing import Any
import datetime
import os
import re
import time
import zoneinfo

from accounts import AccountError, read_account
from discord_client import DiscordError, request

TZ = zoneinfo.ZoneInfo(os.environ.get('DISCORD_MCP_TZ', 'Asia/Tokyo'))
DISCORD_EPOCH_MS = 1420070400000
GUILDS_TTL = 60
CHANNELS_TTL = 60
CHANNEL_TTL = 300
ROLES_TTL = 300
CONCISE_CHARS = 300
WEBHOOK_NAME = 'discord-mcp'

# ボットに要る権限 (ビット, 画面の表示名)。招待 URL と管理画面の権限の確かめに使う
PERMISSIONS = [
    (10, 'チャンネルを見る'), (11, 'メッセージを送信'), (38, 'スレッドでメッセージを送信'), (35, '公開スレッドの作成'),
    (14, '埋め込みリンク'), (15, 'ファイルを添付'), (16, 'メッセージ履歴を読む'), (6, 'リアクションの追加'),
    (18, '外部の絵文字を使用する'), (29, 'ウェブフックの管理 (AI の名前で投稿するため)'),
]
INVITE_PERMISSIONS = sum(1 << bit for bit, _ in PERMISSIONS)
ADMINISTRATOR = 1 << 3


def invite_url(application_id: str) -> str:
    return f'https://discord.com/oauth2/authorize?client_id={application_id}&scope=bot&permissions={INVITE_PERMISSIONS}'

CHANNEL_TYPES = {
    0: 'text', 1: 'dm', 2: 'voice', 3: 'group_dm', 4: 'category', 5: 'announcement',
    10: 'announcement_thread', 11: 'public_thread', 12: 'private_thread', 13: 'stage', 14: 'directory', 15: 'forum', 16: 'media',
}
THREAD_TYPES = (10, 11, 12)
# メッセージの種類 (よくあるものだけ名前にする。ほかは番号のまま)
MESSAGE_TYPES = {0: None, 19: None, 7: 'member_join', 6: 'pin', 18: 'thread_created', 21: 'thread_starter', 8: 'boost', 20: 'command'}

MENTION_USER = re.compile(r'<@!?(\d+)>')
MENTION_ROLE = re.compile(r'<@&(\d+)>')
MENTION_CHANNEL = re.compile(r'<#(\d+)>')
CUSTOM_EMOJI = re.compile(r'<a?:(\w+):\d+>')
TIMESTAMP = re.compile(r'<t:(-?\d+)(?::[tTdDfFsSR])?>')


# ------------------------------------------------------------
# 時刻・ID
# ------------------------------------------------------------
def fmt_time(value: str | None) -> str | None:
    """Discord の ISO 8601 (UTC) を、表示用のタイムゾーンの ISO 8601 にする"""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return value
    return dt.astimezone(TZ).isoformat(timespec='seconds')


def fmt_unix(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, TZ).isoformat(timespec='seconds')


def snowflake_time(snowflake: str) -> str:
    return fmt_unix(((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000)


def to_snowflake(value: str) -> str:
    """メッセージ ID はそのまま、ISO 8601 の日時 (タイムゾーンなしは表示用のタイムゾーン) はその時刻の ID にする"""
    value = (value or '').strip()
    if re.fullmatch(r'\d{15,21}', value):
        return value
    try:
        dt = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f'「{value}」はメッセージ ID でも ISO 8601 の日時 (例 2026-11-15T09:00) でもない') from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    ms = int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS
    if ms < 0:
        raise ValueError(f'「{value}」は Discord ができる前の日時')
    return str(ms << 22)


def is_snowflake(value: Any) -> bool:
    return bool(re.fullmatch(r'\d{15,21}', str(value or '')))


# ------------------------------------------------------------
# ユーザー・チャンネルの表示
# ------------------------------------------------------------
def user_name(user: dict | None, member: dict | None = None) -> str:
    """サーバーでのニックネーム > 表示名 > ユーザー名"""
    if not user:
        return '(不明)'
    return (member or {}).get('nick') or user.get('global_name') or user.get('username') or user.get('id', '(不明)')


def user_view(user: dict, member: dict | None = None) -> dict:
    out = {'id': user.get('id'), 'name': user_name(user, member), 'username': user.get('username')}
    if user.get('bot'):
        out['bot'] = True
    return out


def channel_type(c: dict) -> str:
    return CHANNEL_TYPES.get(c.get('type'), str(c.get('type')))


class Bot:
    """1 つのアカウント (ボット) の状態"""

    def __init__(self, name: str):
        self.name = name
        self._token = ''
        self._reset()

    def _reset(self) -> None:
        self._me: dict | None = None
        self._app: dict | None = None
        self._guilds: tuple[float, list[dict]] | None = None
        self._channels: dict[str, tuple[float, list[dict]]] = {}
        self._channel: dict[str, tuple[float, dict]] = {}
        self._roles: dict[str, tuple[float, dict[str, str]]] = {}
        self._webhooks: dict[str, dict] = {}

    # ---- 設定 ----
    @property
    def config(self) -> dict:
        config = read_account(self.name)
        if config['bot_token'] != self._token:  # トークンを書き換えたら覚えていたものを捨てる
            self._token = config['bot_token']
            self._reset()
        return config

    @property
    def token(self) -> str:
        return self.config['bot_token']

    @property
    def read_only(self) -> bool:
        return self.config['read_only']

    def allowed(self, guild_id: str | None) -> bool:
        """そのサーバーを扱ってよいか (servers の指定がなければ全部。DM はサーバーがないので可)"""
        servers = self.config['servers']
        return not guild_id or not servers or guild_id in servers

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        return await request(self.token, method, path, **kwargs)

    # ---- 自分 ----
    async def me(self) -> dict:
        token = self.token
        if self._me is None:
            self._me = await request(token, 'GET', '/users/@me')
        return self._me

    async def application(self) -> dict:
        token = self.token
        if self._app is None:
            self._app = await request(token, 'GET', '/applications/@me')
        return self._app

    # ---- サーバー ----
    async def guilds(self, refresh: bool = False) -> list[dict]:
        """ボットが入っているサーバーのうち、扱ってよいもの"""
        token = self.token
        now = time.monotonic()
        if refresh or not self._guilds or now - self._guilds[0] > GUILDS_TTL:
            guilds: list[dict] = []
            after = None
            for _ in range(10):  # 1 回 200 件まで
                page = await request(token, 'GET', '/users/@me/guilds', params={'limit': 200, 'after': after, 'with_counts': True})
                guilds.extend(page)
                if len(page) < 200:
                    break
                after = page[-1]['id']
            self._guilds = (now, guilds)
        return [g for g in self._guilds[1] if self.allowed(g['id'])]

    async def guild(self, server: str | None) -> dict:
        """サーバー ID か名前 (大文字小文字を区別しない。一意なら部分一致も) からサーバーを決める。
        省略したときは、扱えるサーバーが 1 つだけならそれ"""
        guilds = await self.guilds()
        if not guilds:
            raise ValueError(f'アカウント「{self.name}」のボットはどのサーバーにも入っていない (discord_list_accounts の invite_url で招待する)')
        if not server:
            if len(guilds) == 1:
                return guilds[0]
            raise ValueError(f'サーバーが複数ある ({", ".join(g["name"] for g in guilds)}) ので、server で ID か名前を指定する')
        server = server.strip()
        if is_snowflake(server):
            for g in guilds:
                if g['id'] == server:
                    return g
            if not self.allowed(server):
                raise ValueError(f'サーバー {server} はこのアカウントでは扱わない設定 (accounts/{self.name}.toml の servers)')
            raise ValueError(f'ボットはサーバー {server} に入っていない (discord_list_servers で確かめる)')
        name = server.lower()
        exact = [g for g in guilds if g['name'].lower() == name]
        partial = exact or [g for g in guilds if name in g['name'].lower()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            listed = ', '.join('%s (%s)' % (g['name'], g['id']) for g in partial)
            raise ValueError(f'「{server}」に当てはまるサーバーが複数ある ({listed})。ID で指定する')
        raise ValueError(f'「{server}」という名前のサーバーはない (discord_list_servers で確かめる)')

    # ---- チャンネル ----
    async def guild_channels(self, guild_id: str) -> list[dict]:
        token = self.token
        now = time.monotonic()
        cached = self._channels.get(guild_id)
        if not cached or now - cached[0] > CHANNELS_TTL:
            channels = await request(token, 'GET', f'/guilds/{guild_id}/channels')
            cached = (now, channels)
            self._channels[guild_id] = cached
            for c in channels:
                self._channel[c['id']] = (now, c)
        return cached[1]

    async def channel(self, channel_id: str) -> dict:
        """チャンネル (スレッド・DM も) を返す。扱わないサーバーのものは断る"""
        channel_id = (channel_id or '').strip().lstrip('#')
        if not is_snowflake(channel_id):
            raise ValueError(f'「{channel_id}」はチャンネル ID (数字) ではない。discord_list_channels で ID を調べる')
        token = self.token
        now = time.monotonic()
        cached = self._channel.get(channel_id)
        if not cached or now - cached[0] > CHANNEL_TTL:
            c = await request(token, 'GET', f'/channels/{channel_id}')
            cached = (now, c)
            self._channel[channel_id] = cached
        c = cached[1]
        if not self.allowed(c.get('guild_id')):
            raise ValueError(f'チャンネル {channel_id} はこのアカウントでは扱わないサーバーのもの (accounts/{self.name}.toml の servers)')
        return c

    def forget_channel(self, channel_id: str) -> None:
        self._channel.pop(channel_id, None)

    async def webhook(self, channel_id: str, create: bool = True) -> dict | None:
        """AI の名前で投稿するための、そのチャンネル (スレッドなら親) の webhook ({'id', 'token'})。
        このアプリが作った webhook があれば使い、なければ作る (create=False なら None)。
        ボットに「ウェブフックの管理」の権限が要る"""
        token = self.token
        hook = self._webhooks.get(channel_id)
        if hook:
            return hook
        app = await self.application()
        hooks = await request(token, 'GET', f'/channels/{channel_id}/webhooks')
        hook = next((h for h in hooks if h.get('type') == 1 and h.get('application_id') == app['id'] and h.get('token')), None)
        if hook is None:
            if not create:
                return None
            hook = await request(token, 'POST', f'/channels/{channel_id}/webhooks', body={'name': WEBHOOK_NAME},
                                 reason='discord-mcp: AI の名前で投稿するため')
        self._webhooks[channel_id] = {'id': hook['id'], 'token': hook['token']}
        return self._webhooks[channel_id]

    def forget_webhook(self, channel_id: str | None) -> None:
        self._webhooks.pop(channel_id or '', None)

    async def roles(self, guild_id: str) -> dict[str, str]:
        token = self.token
        now = time.monotonic()
        cached = self._roles.get(guild_id)
        if not cached or now - cached[0] > ROLES_TTL:
            roles = await request(token, 'GET', f'/guilds/{guild_id}/roles')
            cached = (now, {r['id']: r['name'] for r in roles})
            self._roles[guild_id] = cached
        return cached[1]

    async def names(self, guild_id: str | None) -> dict:
        """本文の置き換えに使う、そのサーバーのチャンネル名・ロール名 (取れなければ空)"""
        if not guild_id:
            return {'guild_id': None, 'channels': {}, 'roles': {}}
        try:
            channels = {c['id']: c.get('name') for c in await self.guild_channels(guild_id)}
        except DiscordError:
            channels = {}
        try:
            roles = await self.roles(guild_id)
        except DiscordError:
            roles = {}
        return {'guild_id': guild_id, 'channels': channels, 'roles': roles}

    async def channel_view(self, c: dict) -> dict:
        out: dict[str, Any] = {'id': c['id'], 'name': c.get('name'), 'type': channel_type(c)}
        if c.get('guild_id'):
            out['server_id'] = c['guild_id']
            parent = c.get('parent_id')
            if parent:
                try:
                    names = {x['id']: x.get('name') for x in await self.guild_channels(c['guild_id'])}
                except DiscordError:
                    names = {}
                out['thread_of' if c.get('type') in THREAD_TYPES else 'category'] = {'id': parent, 'name': names.get(parent)}
        if c.get('type') in (1, 3):
            out['recipients'] = [user_view(u) for u in c.get('recipients', [])]
        if c.get('topic'):
            out['topic'] = c['topic']
        if c.get('nsfw'):
            out['nsfw'] = True
        meta = c.get('thread_metadata')
        if meta:
            out['archived'] = bool(meta.get('archived'))
            if meta.get('locked'):
                out['locked'] = True
            out['message_count'] = c.get('message_count')
        return {k: v for k, v in out.items() if v is not None}


# ------------------------------------------------------------
# メッセージの整形
# ------------------------------------------------------------
def render_text(content: str, msg: dict, names: dict) -> str:
    """<@id> などを名前に置き換える"""
    if not content:
        return ''
    users = {}
    for u in msg.get('mentions', []) or []:
        users[u['id']] = user_name(u, u.get('member'))
    if msg.get('author'):
        users.setdefault(msg['author']['id'], user_name(msg['author'], msg.get('member')))
    text = MENTION_USER.sub(lambda m: '@' + users.get(m.group(1), m.group(1)), content)
    text = MENTION_ROLE.sub(lambda m: '@' + (names['roles'].get(m.group(1)) or m.group(1)), text)
    text = MENTION_CHANNEL.sub(lambda m: '#' + (names['channels'].get(m.group(1)) or m.group(1)), text)
    text = CUSTOM_EMOJI.sub(lambda m: f':{m.group(1)}:', text)
    return TIMESTAMP.sub(lambda m: fmt_unix(int(m.group(1))), text)


def emoji_text(emoji: dict) -> str:
    return f':{emoji["name"]}:' if emoji.get('id') else (emoji.get('name') or '')


def jump_url(guild_id: str | None, channel_id: str, message_id: str) -> str:
    return f'https://discord.com/channels/{guild_id or "@me"}/{channel_id}/{message_id}'


def attachment_view(a: dict, with_url: bool = False) -> dict:
    out = {'id': a.get('id'), 'filename': a.get('filename'), 'content_type': a.get('content_type'), 'size': a.get('size')}
    if a.get('width'):
        out['image_size'] = f'{a["width"]}x{a.get("height")}'
    if a.get('description'):
        out['description'] = a['description']
    if with_url:
        out['url'] = a.get('url')
    return {k: v for k, v in out.items() if v is not None}


def embed_view(e: dict, full: bool) -> dict:
    out: dict[str, Any] = {k: e.get(k) for k in ('type', 'title', 'url', 'description') if e.get(k)}
    if e.get('author', {}).get('name'):
        out['author'] = e['author']['name']
    if full:
        if e.get('fields'):
            out['fields'] = [{'name': f.get('name'), 'value': f.get('value')} for f in e['fields']]
        if e.get('footer', {}).get('text'):
            out['footer'] = e['footer']['text']
        if e.get('image', {}).get('url'):
            out['image'] = e['image']['url']
    elif out.get('description') and len(out['description']) > CONCISE_CHARS:
        out['description'] = out['description'][:CONCISE_CHARS] + '…'
    return out


def message_view(msg: dict, names: dict, response_format: str = 'detailed', full: bool = False) -> dict:
    """メッセージを AI が読みやすい形にする。response_format=concise は本文を短く、要点だけ。full は埋め込みの中身・添付の URL まで"""
    guild_id = names.get('guild_id') or msg.get('guild_id')
    author = msg.get('author') or {}
    text = render_text(msg.get('content', ''), msg, names)
    concise = response_format == 'concise'
    out: dict[str, Any] = {'id': msg['id'], 'author': user_name(author, msg.get('member')) if concise else user_view(author, msg.get('member')),
                           'time': fmt_time(msg.get('timestamp'))}
    kind = MESSAGE_TYPES.get(msg.get('type', 0), str(msg.get('type')))
    if kind:
        out['kind'] = kind
    if concise and len(text) > CONCISE_CHARS:
        text = text[:CONCISE_CHARS] + '…'
    if text:
        out['text'] = text
    elif not (msg.get('attachments') or msg.get('embeds') or msg.get('sticker_items') or msg.get('message_snapshots') or msg.get('poll')) \
            and msg.get('type', 0) in (0, 19):
        out['content_missing'] = True
    ref = msg.get('referenced_message')
    if ref:
        reply = {'id': ref['id'], 'author': user_name(ref.get('author'), ref.get('member'))}
        ref_text = render_text(ref.get('content', ''), ref, names)
        if ref_text:
            reply['text'] = ref_text[:120] + ('…' if len(ref_text) > 120 else '')
        out['reply_to'] = reply
    elif msg.get('message_reference', {}).get('message_id') and msg.get('type', 0) == 19:
        out['reply_to'] = {'id': msg['message_reference']['message_id'], 'note': '元のメッセージは削除済み'}
    snapshots = msg.get('message_snapshots') or []
    if snapshots:  # 転送されたメッセージ
        fwd = snapshots[0].get('message', {})
        out['forwarded'] = {k: v for k, v in {
            'text': render_text(fwd.get('content', ''), fwd, names) or None,
            'attachments': [attachment_view(a, full) for a in fwd.get('attachments', [])] or None,
            'time': fmt_time(fwd.get('timestamp')),
        }.items() if v}
    if msg.get('attachments'):
        out['attachments'] = [a.get('filename') for a in msg['attachments']] if concise else \
            [attachment_view(a, full) for a in msg['attachments']]
    if msg.get('embeds') and not concise:
        out['embeds'] = [embed_view(e, full) for e in msg['embeds']]
    if msg.get('sticker_items'):
        out['stickers'] = [s.get('name') for s in msg['sticker_items']]
    if msg.get('poll'):
        poll = msg['poll']
        out['poll'] = {'question': poll.get('question', {}).get('text'),
                       'answers': [a.get('poll_media', {}).get('text') for a in poll.get('answers', [])]}
    if msg.get('reactions'):
        out['reactions'] = ' '.join(f'{emoji_text(r["emoji"])}×{r["count"]}' for r in msg['reactions']) if concise else \
            [{'emoji': emoji_text(r['emoji']), 'count': r['count'], **({'me': True} if r.get('me') else {})} for r in msg['reactions']]
    thread = msg.get('thread')
    if thread:
        out['thread'] = {'id': thread['id'], 'name': thread.get('name'), 'message_count': thread.get('message_count')}
    if msg.get('edited_timestamp') and not concise:
        out['edited'] = fmt_time(msg['edited_timestamp'])
    if msg.get('pinned') and not concise:
        out['pinned'] = True
    if not concise:
        out['url'] = jump_url(guild_id, msg['channel_id'], msg['id'])
    return out


def has_missing_content(views: list[dict]) -> bool:
    return any(v.get('content_missing') for v in views)


CONTENT_NOTE = ('本文が空のメッセージがある (content_missing)。ボットの Message Content Intent がオフだと、'
                'ボット宛てでないメッセージの本文は空になる。持ち主に Developer Portal の Bot ページでオンにしてもらう')


__all__ = ['AccountError', 'Bot', 'CONTENT_NOTE', 'THREAD_TYPES', 'attachment_view', 'channel_type', 'emoji_text', 'fmt_time',
           'has_missing_content', 'is_snowflake', 'message_view', 'snowflake_time', 'to_snowflake', 'user_name', 'user_view']
