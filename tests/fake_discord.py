"""通し試験用の偽の Discord REST API (ASGI)。実際の Discord には接続しない。

2 つのボット (トークン TOKEN_A / TOKEN_B) を持ち、ボット A は 2 つのサーバー、ボット B は 1 つのサーバーに入っている。
受け取ったリクエストは calls に、送られたメッセージなどは state に記録する (試験で引数を確かめるため)。
"""
from email.parser import BytesParser
from email.policy import default as email_policy
from urllib.parse import parse_qs, unquote
import json
import re

TOKEN_A = 'TESTBOTAAAAAAAAAAAA.abcdef.aaaaaaaaaaaaaaaaaaaaaaaa'
TOKEN_B = 'TESTBOTBBBBBBBBBBBB.abcdef.bbbbbbbbbbbbbbbbbbbbbbbb'

BOTS = {
    TOKEN_A: {'id': '900000000000000001', 'username': 'helper-a', 'global_name': None, 'bot': True},
    TOKEN_B: {'id': '900000000000000002', 'username': 'helper-b', 'global_name': None, 'bot': True},
}
GUILDS_OF = {TOKEN_A: ['100000000000000001', '100000000000000002'], TOKEN_B: ['100000000000000003']}
GUILDS = {
    '100000000000000001': {'id': '100000000000000001', 'name': 'ゲーム仲間', 'approximate_member_count': 12},
    '100000000000000002': {'id': '100000000000000002', 'name': 'Work Lab', 'approximate_member_count': 40},
    '100000000000000003': {'id': '100000000000000003', 'name': 'Other', 'approximate_member_count': 3},
}
G1, G2, G3 = '100000000000000001', '100000000000000002', '100000000000000003'

USERS = {
    '800000000000000001': {'id': '800000000000000001', 'username': 'taro', 'global_name': '太郎'},
    '800000000000000002': {'id': '800000000000000002', 'username': 'hanako', 'global_name': None},
    '900000000000000001': BOTS[TOKEN_A],
}
MEMBERS = {
    G1: {
        '800000000000000001': {'user': USERS['800000000000000001'], 'nick': 'たろう', 'roles': ['300000000000000001'], 'joined_at': '2025-01-01T00:00:00+00:00'},
        '800000000000000002': {'user': USERS['800000000000000002'], 'nick': None, 'roles': [], 'joined_at': '2025-02-01T00:00:00+00:00'},
    },
}
_BASIC = sum(1 << b for b in (6, 10, 11, 14, 15, 16, 18, 35, 38))  # ウェブフックの管理だけない
ROLES = {
    G1: [{'id': '300000000000000001', 'name': '管理人', 'permissions': str(1 << 3)},
         {'id': '300000000000000002', 'name': 'helper', 'permissions': str(_BASIC & ~(1 << 10))},
         {'id': G1, 'name': '@everyone', 'permissions': str(1 << 10)}],
    G2: [{'id': '300000000000000003', 'name': 'bots', 'permissions': str(1 << 3)}, {'id': G2, 'name': '@everyone', 'permissions': '0'}],
    G3: [{'id': G3, 'name': '@everyone', 'permissions': '0'}],
}
# ボット自身のメンバー情報 (管理画面の権限の確かめに使う)
BOT_MEMBERS = {
    (G1, '900000000000000001'): {'user': BOTS[TOKEN_A], 'roles': ['300000000000000002']},
    (G2, '900000000000000001'): {'user': BOTS[TOKEN_A], 'roles': ['300000000000000003']},
    (G3, '900000000000000002'): {'user': BOTS[TOKEN_B], 'roles': []},
}
# アプリケーションの flags と公開設定 (A は Message Content・Server Members の intent がオン)
APPS = {TOKEN_A: {'flags': (1 << 19) | (1 << 15), 'bot_public': False}, TOKEN_B: {'flags': 0, 'bot_public': True}}

C_GENERAL, C_FORUM, C_VOICE, C_CAT, C_THREAD, C_WORK, C_OTHER, C_DM = (
    '200000000000000001', '200000000000000002', '200000000000000003', '200000000000000004',
    '200000000000000005', '200000000000000006', '200000000000000007', '200000000000000008')
CHANNELS = {
    C_CAT: {'id': C_CAT, 'guild_id': G1, 'type': 4, 'name': 'テキスト', 'position': 0},
    C_GENERAL: {'id': C_GENERAL, 'guild_id': G1, 'type': 0, 'name': '雑談', 'parent_id': C_CAT, 'position': 0, 'topic': '何でも'},
    C_FORUM: {'id': C_FORUM, 'guild_id': G1, 'type': 15, 'name': '質問', 'parent_id': C_CAT, 'position': 1},
    C_VOICE: {'id': C_VOICE, 'guild_id': G1, 'type': 2, 'name': 'ボイス', 'position': 2},
    C_WORK: {'id': C_WORK, 'guild_id': G2, 'type': 0, 'name': 'general', 'position': 0},
    C_OTHER: {'id': C_OTHER, 'guild_id': G3, 'type': 0, 'name': 'general', 'position': 0},
}
THREADS = {
    C_THREAD: {'id': C_THREAD, 'guild_id': G1, 'type': 11, 'name': '週末の予定', 'parent_id': C_GENERAL, 'message_count': 2,
               'thread_metadata': {'archived': False, 'create_timestamp': '2026-11-01T00:00:00+00:00'}},
}
ARCHIVED = {C_GENERAL: [{'id': '200000000000000009', 'guild_id': G1, 'type': 11, 'name': '古い話', 'parent_id': C_GENERAL,
                         'message_count': 5, 'thread_metadata': {'archived': True, 'archive_timestamp': '2026-10-01T00:00:00+00:00'}}]}
DM_CHANNEL = {'id': C_DM, 'type': 1, 'recipients': [USERS['800000000000000001']]}

EMOJIS = {G1: [{'id': '400000000000000001', 'name': 'party', 'animated': False}], G2: [], G3: []}


def _msg(mid, channel, author, content, **extra):
    return {'id': mid, 'channel_id': channel, 'author': author, 'content': content, 'type': extra.pop('type', 0),
            'timestamp': extra.pop('timestamp', '2026-11-15T03:00:00+00:00'), 'edited_timestamp': None,
            'mentions': extra.pop('mentions', []), 'attachments': extra.pop('attachments', []), 'embeds': extra.pop('embeds', []),
            **extra}


BASE = {'url': ''}
MESSAGES = {
    C_GENERAL: [
        _msg('1300000000000000001', C_GENERAL, USERS['800000000000000002'], 'おはよう <:party:400000000000000001>', timestamp='2026-11-15T00:00:00+00:00'),
        _msg('1300000000000000002', C_GENERAL, USERS['800000000000000001'],
             '<@900000000000000001> <@&300000000000000001> <#200000000000000004> 見て <t:1763164800:f>',
             member={'nick': 'たろう'}, mentions=[dict(BOTS[TOKEN_A], member={'nick': None})],
             reactions=[{'emoji': {'id': None, 'name': '👍'}, 'count': 2, 'me': False},
                        {'emoji': {'id': '400000000000000001', 'name': 'party'}, 'count': 1, 'me': True}],
             thread={'id': C_THREAD, 'name': '週末の予定', 'message_count': 2},
             attachments=[{'id': '500000000000000001', 'filename': 'memo.txt', 'content_type': 'text/plain; charset=utf-8', 'size': 18, 'url': '{base}/cdn/memo.txt'},
                          {'id': '500000000000000002', 'filename': 'dot.png', 'content_type': 'image/png', 'size': 70, 'url': '{base}/cdn/dot.png', 'width': 1, 'height': 1}],
             embeds=[{'type': 'rich', 'title': 'お知らせ', 'description': '詳細', 'fields': [{'name': 'a', 'value': 'b'}]}]),
        _msg('1300000000000000003', C_GENERAL, USERS['800000000000000002'], '', type=0),  # Message Content Intent がない時の形
        _msg('1300000000000000004', C_GENERAL, BOTS[TOKEN_A], 'ボットの発言'),
    ],
    C_THREAD: [_msg('1300000000000000010', C_THREAD, USERS['800000000000000001'], '土曜はどう？')],
    C_WORK: [_msg('1300000000000000020', C_WORK, USERS['800000000000000002'], 'work')],
    C_OTHER: [_msg('1300000000000000030', C_OTHER, USERS['800000000000000002'], 'other')],
    C_DM: [],
}
PINS = {C_GENERAL: ['1300000000000000002']}
FILE_BODIES = {'memo.txt': ('メモの中身です\n'.encode(), 'text/plain'),
               'dot.png': (b'\x89PNG\r\n\x1a\n' + b'\x00' * 62, 'image/png')}

# チャンネルごとの webhook。質問 (フォーラム) にはこのアプリが作ったものと、ほかのアプリのものが最初からある
WEBHOOKS = {
    C_FORUM: [{'id': '600000000000000001', 'token': 'hooktoken-forum', 'type': 1, 'application_id': BOTS[TOKEN_A]['id'], 'channel_id': C_FORUM},
              {'id': '600000000000000002', 'token': 'hooktoken-other', 'type': 1, 'application_id': '999', 'channel_id': C_FORUM}],
}
NO_WEBHOOK_PERMISSION = {C_WORK}

calls: list[tuple[str, str, dict, object]] = []
state = {'sent': [], 'reactions': [], 'deleted': [], 'edited': [], 'threads': [], 'uploads': [], 'webhooks_created': [],
         'ratelimit_once': set(), 'next_id': 1400000000000000000}


def new_id() -> str:
    state['next_id'] += 1
    return str(state['next_id'])


def guild_of(channel_id):
    c = CHANNELS.get(channel_id) or THREADS.get(channel_id)
    return c.get('guild_id') if c else None


def visible(token, channel_id) -> bool:
    if channel_id == C_DM:
        return token == TOKEN_A
    return guild_of(channel_id) in GUILDS_OF[token]


def materialize(m):
    out = json.loads(json.dumps(m).replace('{base}', BASE['url']))
    gid = guild_of(m['channel_id'])
    if gid:
        out['guild_id'] = gid
    return out


def find_message(channel_id, mid):
    for m in MESSAGES.get(channel_id, []):
        if m['id'] == mid:
            return m
    return None


def E(status, code, message):
    return status, {'code': code, 'message': message}


def handle(token, method, path, query, body):  # noqa: C901 (試験用なので 1 つの関数にまとめる)
    q = {k: v if len(v) > 1 else v[0] for k, v in query.items()}
    if path == '/users/@me':
        return 200, BOTS[token]
    if path == '/applications/@me':
        return 200, {'id': BOTS[token]['id'], 'name': BOTS[token]['username'], **APPS[token]}
    if path == '/users/@me/guilds':
        return 200, [GUILDS[g] for g in GUILDS_OF[token]]
    if path == '/users/@me/channels' and method == 'POST':
        if body.get('recipient_id') not in USERS:
            return E(400, 50007, 'Cannot send messages to this user')
        return 200, DM_CHANNEL
    m = re.fullmatch(r'/users/(\d+)', path)
    if m:
        return (200, USERS[m.group(1)]) if m.group(1) in USERS else E(404, 10013, 'Unknown User')

    m = re.fullmatch(r'/guilds/(\d+)(/.*)', path)
    if m:
        gid, rest = m.groups()
        if gid not in GUILDS_OF[token]:
            return E(404, 10004, 'Unknown Guild')
        if rest == '/channels':
            return 200, [c for c in CHANNELS.values() if c['guild_id'] == gid]
        if rest == '/threads/active':
            return 200, {'threads': [t for t in THREADS.values() if t['guild_id'] == gid]}
        if rest == '/roles':
            return 200, ROLES[gid]
        if rest == '/emojis':
            return 200, EMOJIS[gid]
        if rest == '/members/search':
            word = q.get('query', '').lower()
            return 200, [mb for mb in MEMBERS.get(gid, {}).values()
                         if mb['user']['username'].startswith(word) or (mb.get('nick') or '').startswith(word)]
        if rest == '/members':
            return 200, list(MEMBERS.get(gid, {}).values())[: int(q.get('limit', 1))]
        mm = re.fullmatch(r'/members/(\d+)', rest)
        if mm:
            member = BOT_MEMBERS.get((gid, mm.group(1))) or MEMBERS.get(gid, {}).get(mm.group(1))
            return (200, member) if member else E(404, 10007, 'Unknown Member')
        if rest == '/messages/search':
            hits = []
            for cid, msgs in MESSAGES.items():
                if guild_of(cid) != gid:
                    continue
                if 'channel_id' in q and cid not in (q['channel_id'] if isinstance(q['channel_id'], list) else [q['channel_id']]):
                    continue
                for msg in msgs:
                    if 'content' in q and q['content'] not in msg['content']:
                        continue
                    if 'author_id' in q and msg['author']['id'] not in (q['author_id'] if isinstance(q['author_id'], list) else [q['author_id']]):
                        continue
                    hits.append([materialize(msg)])
            hits.sort(key=lambda h: -int(h[0]['id']))
            return 200, {'total_results': len(hits), 'messages': hits[: int(q.get('limit', 25))],
                         'threads': [t for t in THREADS.values() if t['guild_id'] == gid], 'doing_deep_historical_index': False}

    m = re.fullmatch(r'/channels/(\d+)(/.*)?', path)
    if m:
        cid, rest = m.group(1), m.group(2) or ''
        channel = CHANNELS.get(cid) or THREADS.get(cid) or (DM_CHANNEL if cid == C_DM else None)
        if channel is None or not visible(token, cid):
            return E(404, 10003, 'Unknown Channel')
        if rest == '':
            return 200, channel
        if rest == '/messages' and method == 'GET':
            if cid in state['ratelimit_once']:
                state['ratelimit_once'].discard(cid)
                return 429, {'message': 'You are being rate limited.', 'retry_after': 0.2, 'global': False}
            msgs = sorted(MESSAGES.get(cid, []), key=lambda x: -int(x['id']))
            if 'before' in q:
                msgs = [x for x in msgs if int(x['id']) < int(q['before'])]
            if 'after' in q:
                msgs = sorted([x for x in msgs if int(x['id']) > int(q['after'])], key=lambda x: int(x['id']))
            return 200, [materialize(x) for x in msgs[: int(q.get('limit', 50))]]
        if rest == '/messages' and method == 'POST':
            if channel.get('type') == 2:
                return E(400, 50008, 'Cannot send messages in a non-text channel')
            content = body.get('content', '')
            if len(content) > 2000:
                return E(400, 50035, 'Invalid Form Body')
            files = body.pop('__files__', [])
            msg = _msg(new_id(), cid, BOTS[token], content,
                       attachments=[{'id': new_id(), 'filename': f['filename'], 'size': len(f['data']), 'content_type': f['type']} for f in files])
            state['sent'].append({'channel': cid, 'body': body, 'files': files})
            MESSAGES.setdefault(cid, []).append(msg)
            return 200, materialize(msg)
        if rest == '/webhooks':
            if cid in NO_WEBHOOK_PERMISSION:
                return E(403, 50013, 'Missing Permissions')
            if method == 'GET':
                return 200, WEBHOOKS.get(cid, [])
            hook = {'id': new_id(), 'token': f'hooktoken-{cid}', 'type': 1, 'application_id': BOTS[token]['id'],
                    'channel_id': cid, 'name': body.get('name')}
            WEBHOOKS.setdefault(cid, []).append(hook)
            state['webhooks_created'].append(cid)
            return 200, hook
        if rest == '/messages/pins':
            return 200, {'items': [{'pinned_at': '2026-11-15T05:00:00+00:00', 'message': materialize(find_message(cid, i))} for i in PINS.get(cid, [])],
                         'has_more': False}
        if rest == '/threads/archived/public':
            return 200, {'threads': ARCHIVED.get(cid, []), 'has_more': False}
        if rest == '/threads' and method == 'POST':
            tid = new_id()
            thread = {'id': tid, 'guild_id': channel['guild_id'], 'type': 11, 'name': body['name'], 'parent_id': cid,
                      'thread_metadata': {'archived': False}}
            THREADS[tid] = thread
            MESSAGES[tid] = []
            state['threads'].append({'channel': cid, 'body': body})
            return 201, thread
        mm = re.fullmatch(r'/messages/(\d+)(/.*)?', rest)
        if mm:
            mid, sub = mm.group(1), mm.group(2) or ''
            msg = find_message(cid, mid)
            if msg is None:
                return E(404, 10008, 'Unknown Message')
            if sub == '' and method == 'GET':
                return 200, materialize(msg)
            if sub == '' and method == 'PATCH':
                if msg['author']['id'] != BOTS[token]['id']:
                    return E(403, 50005, 'Cannot edit a message authored by another user')
                msg['content'] = body['content']
                msg['edited_timestamp'] = '2026-11-15T06:00:00+00:00'
                state['edited'].append(mid)
                return 200, materialize(msg)
            if sub == '' and method == 'DELETE':
                MESSAGES[cid].remove(msg)
                state['deleted'].append(mid)
                return 204, None
            if sub == '/threads' and method == 'POST':
                tid = new_id()
                THREADS[tid] = {'id': tid, 'guild_id': channel['guild_id'], 'type': 11, 'name': body['name'], 'parent_id': cid}
                MESSAGES[tid] = []
                state['threads'].append({'channel': cid, 'message': mid, 'body': body})
                return 201, THREADS[tid]
            rm = re.fullmatch(r'/reactions/([^/]+)(/@me)?', sub)
            if rm:
                emoji = unquote(rm.group(1))
                known = emoji in ('👍', '🎉') or emoji == 'party:400000000000000001'
                if not known:
                    return E(400, 10014, 'Unknown Emoji')
                if rm.group(2):
                    state['reactions'].append((method, mid, emoji))
                    return 204, None
                return 200, [USERS['800000000000000001'], USERS['800000000000000002']][: 2 if emoji == '👍' else 1]
    return E(404, 0, f'404: Not Found ({method} {path})')


def handle_webhook(method, path, query, body, authorized):
    """/webhooks/{id}/{token}... (URL のトークンで認証する。Authorization は付けない)"""
    q = {k: v[0] for k, v in query.items()}
    m = re.fullmatch(r'/webhooks/(\d+)/([\w-]+)(?:/messages/(\d+))?', path)
    hook = None
    if m:
        hook = next((h for hooks in WEBHOOKS.values() for h in hooks if h['id'] == m.group(1) and h['token'] == m.group(2)), None)
    if hook is None:
        return E(404, 10015, 'Unknown Webhook')
    if authorized:
        return E(400, 0, 'webhook の実行に Authorization を付けている')
    target = q.get('thread_id') or hook['channel_id']
    if m.group(3):
        msg = find_message(target, m.group(3))
        if msg is None or msg.get('webhook_id') != hook['id']:
            return E(404, 10008, 'Unknown Message')
        if method == 'PATCH':
            msg['content'] = body['content']
            msg['edited_timestamp'] = '2026-11-15T06:00:00+00:00'
            state['edited'].append(msg['id'])
            return 200, materialize(msg)
        if method == 'DELETE':
            MESSAGES[target].remove(msg)
            state['deleted'].append(msg['id'])
            return 204, None
    if method != 'POST' or q.get('wait') != 'true':
        return E(400, 0, 'wait=true で実行する')
    if 'message_reference' in body:
        return E(400, 50035, 'webhook は返信を作れない')
    if body.get('thread_name'):
        tid = new_id()
        THREADS[tid] = {'id': tid, 'guild_id': CHANNELS[hook['channel_id']]['guild_id'], 'type': 11, 'name': body['thread_name'],
                        'parent_id': hook['channel_id']}
        MESSAGES[tid] = []
        target = tid
    files = body.pop('__files__', [])
    author = {'id': hook['id'], 'username': body.get('username', 'hook'), 'bot': True}
    msg = _msg(new_id(), target, author, body.get('content', ''), webhook_id=hook['id'],
               attachments=[{'id': new_id(), 'filename': f['filename'], 'size': len(f['data']), 'content_type': f['type']} for f in files])
    state['sent'].append({'channel': target, 'body': body, 'files': files, 'via': 'webhook', 'hook': hook['id']})
    MESSAGES.setdefault(target, []).append(msg)
    return 200, materialize(msg)


async def app(scope, receive, send):
    if scope['type'] != 'http':
        return
    path = scope['path']
    raw = b''
    while True:
        msg = await receive()
        raw += msg.get('body', b'')
        if not msg.get('more_body'):
            break
    headers = {k.decode(): v.decode() for k, v in scope['headers']}
    if path.startswith('/cdn/'):
        data, ctype = FILE_BODIES[path.split('/')[-1]]
        return await respond(send, 200, data, ctype)
    path = path.split('/api', 1)[-1]
    query = parse_qs(scope.get('query_string', b'').decode())
    ctype = headers.get('content-type', '')
    body: object = None
    if ctype.startswith('multipart/form-data'):
        parsed = BytesParser(policy=email_policy).parsebytes(b'Content-Type: ' + ctype.encode() + b'\r\n\r\n' + raw)
        files = []
        for part in parsed.iter_parts():
            name = part.get_param('name', header='content-disposition')
            if name == 'payload_json':
                body = json.loads(part.get_payload(decode=True).decode('utf-8'))
            else:
                files.append({'field': name, 'filename': part.get_filename(), 'type': part.get_content_type(),
                              'data': part.get_payload(decode=True)})
        body = dict(body or {}, __files__=files)
    elif raw:
        body = json.loads(raw)
    calls.append((scope['method'], path, {k: v for k, v in query.items()}, body))
    token = headers.get('authorization', '')
    if path.startswith('/webhooks/'):
        status, data = handle_webhook(scope['method'], path, query, body if isinstance(body, dict) else {}, bool(token))
    elif not token.startswith('Bot ') or token[4:] not in BOTS:
        status, data = 401, {'code': 0, 'message': '401: Unauthorized'}
    elif not headers.get('user-agent', '').startswith('DiscordBot ('):
        status, data = 400, {'code': 0, 'message': 'bad user agent'}
    else:
        status, data = handle(token[4:], scope['method'], path, query, body if isinstance(body, dict) else {})
    out = b'' if data is None else json.dumps(data).encode()
    await respond(send, status, out, 'application/json')


async def respond(send, status, data, ctype):
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', ctype.encode()), (b'content-length', str(len(data)).encode())]})
    await send({'type': 'http.response.body', 'body': data})
