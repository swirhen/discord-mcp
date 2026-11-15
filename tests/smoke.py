"""通し試験: 偽の Discord API と MCP サーバーを同じプロセスで立て、MCP クライアントからツールを呼ぶ。

実行 (リポジトリ直下で):  python tests/smoke.py
実際の Discord には接続しない。
"""
import asyncio
import base64
import json
import logging
import os
import pathlib
import sys
import tempfile
import time

import uvicorn

logging.basicConfig(level=logging.ERROR)

ROOT = pathlib.Path(__file__).resolve().parent.parent
DISCORD_PORT = 18100
MCP_PORT = 18010
TOKEN = 'ab' * 32          # tester: ボット自身の名前で投稿する
TOKEN_CLAUDE = 'c1' * 32   # claude: webhook で「Claude」として投稿する
TOKEN_VIEWER = 'd2' * 32   # viewer: 読み取り専用、アカウント sub だけ
TOKEN_LEGACY = 'e3' * 32   # legacy: 表示名に Discord が断る文字が入っている

sys.path.insert(0, str(ROOT / 'tests'))
import fake_discord as fd  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp(prefix='discord-mcp-test-'))
(tmp / 'accounts').mkdir()
(tmp / 'accounts' / 'main.toml').write_text(f'bot_token = "{fd.TOKEN_A}"\nlabel = "メインのボット"\n', encoding='utf-8')
(tmp / 'accounts' / 'sub.toml').write_text(f'bot_token = "{fd.TOKEN_B}"\nread_only = true\n', encoding='utf-8')
(tmp / 'clients.json').write_text(json.dumps({'clients': [
    {'id': 'tester', 'token': TOKEN, 'name': 'Tester', 'post_as': 'bot'},
    {'id': 'claude', 'token': TOKEN_CLAUDE, 'name': 'Claude', 'avatar_url': 'https://example.com/claude.png'},
    {'id': 'viewer', 'token': TOKEN_VIEWER, 'read_only': True, 'accounts': ['sub']},
    {'id': 'BROKEN', 'token': 'zz'},
    # 名前の検証を入れる前に保存された想定 (Discord が断る文字が入っている)
    {'id': 'legacy', 'token': TOKEN_LEGACY, 'name': 'Discord Bot: v2'},
]}), encoding='utf-8')
os.environ['DISCORD_API_BASE'] = f'http://127.0.0.1:{DISCORD_PORT}/api'
os.environ['DISCORD_MCP_ACCOUNTS_DIR'] = str(tmp / 'accounts')
os.environ['DISCORD_MCP_CLIENTS_FILE'] = str(tmp / 'clients.json')
os.environ['DISCORD_MCP_PORT'] = str(MCP_PORT)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'mcp_server'))
import bot  # noqa: E402
import server  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.types import ImageContent, TextContent  # noqa: E402

fd.BASE['url'] = f'http://127.0.0.1:{DISCORD_PORT}'
failures = []
G1, G2 = fd.G1, fd.G2


def check(name, cond, info=''):
    print(('  ok   ' if cond else '  FAIL ') + name + (f'  {info}' if info and not cond else ''))
    if not cond:
        failures.append(name)


async def call(session, tool_name, raw=False, **args):
    res = await session.call_tool(tool_name, args)
    if res.is_error:
        return {'error': res.content[0].text}
    if raw:
        return res.content
    if res.structured_content:
        return res.structured_content.get('result', res.structured_content)
    if len(res.content) == 1 and isinstance(res.content[0], TextContent):
        try:
            return json.loads(res.content[0].text)
        except ValueError:
            return res.content[0].text
    return res.content


def last_call(method, path_part):
    for c in reversed(fd.calls):
        if c[0] == method and path_part in c[1]:
            return c
    return None


async def run_tests():  # noqa: C901
    async with streamable_http_client(f'http://127.0.0.1:{MCP_PORT}/mcp/{TOKEN}') as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            check('20 tools listed', len(tools) == 20, len(tools))
            check('all tools prefixed', all(t.name.startswith('discord_') for t in tools))

            # アカウント・サーバー
            r = await call(session, 'discord_list_accounts')
            acc = {a['name']: a for a in r['accounts']}
            check('accounts: both listed', set(acc) == {'main', 'sub'}, r)
            check('accounts: bot user', acc['main']['bot']['username'] == 'helper-a' and acc['main']['server_count'] == 2, acc['main'])
            check('accounts: invite url', 'client_id=900000000000000001' in acc['main'].get('invite_url', '')
                  and f'permissions={bot.INVITE_PERMISSIONS}' in acc['main']['invite_url'], acc['main'])
            check('accounts: read_only', acc['sub']['read_only'] is True and acc['main']['read_only'] is False)
            check('accounts: client info', r['client']['id'] == 'tester' and r['client']['post_as'] == 'bot', r['client'])
            r = await call(session, 'discord_list_servers')
            check('account required when several', 'account' in r.get('error', ''), r)
            r = await call(session, 'discord_list_servers', account='main')
            check('servers listed', [s['name'] for s in r['servers']] == ['ゲーム仲間', 'Work Lab'] and r['servers'][0]['members'] == 12, r)

            # チャンネル
            r = await call(session, 'discord_list_channels', account='main')
            check('server required when several', 'server' in r.get('error', ''), r)
            r = await call(session, 'discord_list_channels', account='main', server='ゲーム')
            names = [c['name'] for c in r.get('channels', [])]
            check('channels: partial server name, no voice/category', names == ['雑談', '質問'], r)
            check('channels: category and topic', r['channels'][0]['category'] == 'テキスト' and r['channels'][0]['topic'] == '何でも', r)
            check('channels: active threads', r['active_threads'][0]['name'] == '週末の予定'
                  and r['active_threads'][0]['thread_of']['name'] == '雑談', r)
            r = await call(session, 'discord_list_channels', account='main', server=G1, include_voice=True, query='ボイス')
            check('channels: voice + query', [c['name'] for c in r['channels']] == ['ボイス'] and r['active_threads'] == [], r)
            r = await call(session, 'discord_list_channels', account='main', server='nothing')
            check('channels: unknown server name', 'ない' in r.get('error', ''), r)
            r = await call(session, 'discord_list_threads', account='main', channel_id=fd.C_GENERAL, archived=True)
            check('threads: active + archived', [t['name'] for t in r['active']] == ['週末の予定']
                  and r['archived'][0]['name'] == '古い話' and r['archived'][0]['archived'] is True, r)

            # メッセージを読む
            r = await call(session, 'discord_read_channel', account='main', channel_id=fd.C_GENERAL)
            msgs = r['messages']
            check('read: chronological', [m['id'] for m in msgs] == sorted(m['id'] for m in msgs) and len(msgs) == 4, r)
            check('read: custom emoji', msgs[0]['text'] == 'おはよう :party:', msgs[0])
            m2 = msgs[1]
            check('read: mentions rendered', m2['text'] == '@helper-a @管理人 #テキスト 見て 2025-11-15T09:00:00+09:00', m2.get('text'))
            check('read: nick as author', m2['author']['name'] == 'たろう', m2['author'])
            check('read: reactions', m2['reactions'] == [{'emoji': '👍', 'count': 2}, {'emoji': ':party:', 'count': 1, 'me': True}], m2)
            check('read: thread + attachments + url', m2['thread']['id'] == fd.C_THREAD and len(m2['attachments']) == 2
                  and 'url' not in m2['attachments'][0] and m2['url'].endswith(f'/{G1}/{fd.C_GENERAL}/1300000000000000002'), m2)
            check('read: time in JST', m2['time'] == '2026-11-15T12:00:00+09:00', m2['time'])
            check('read: missing content note', msgs[2].get('content_missing') and 'Message Content Intent' in r.get('note', ''), r)
            check('read: paging hints', r['older_before'] == '1300000000000000001' and r['newer_after'] == '1300000000000000004', r)
            check('read: channel info', r['channel']['name'] == '雑談' and r['channel']['category']['name'] == 'テキスト', r['channel'])
            r = await call(session, 'discord_read_channel', account='main', channel_id=fd.C_GENERAL, response_format='concise', limit=2)
            check('read: concise', r['messages'][-1]['author'] == 'helper-a' and 'url' not in r['messages'][-1], r)
            r = await call(session, 'discord_read_channel', account='main', channel_id=fd.C_GENERAL, before='2026-11-15T09:30')
            c = last_call('GET', f'/channels/{fd.C_GENERAL}/messages')
            check('read: before ISO -> snowflake', c and c[2].get('before', [''])[0].isdigit() and len(c[2]['before'][0]) >= 18, c)
            r = await call(session, 'discord_read_channel', account='main', channel_id=fd.C_GENERAL, before='x', after='y')
            check('read: before+after rejected', 'どれか 1 つ' in r.get('error', ''), r)
            fd.state['ratelimit_once'].add(fd.C_THREAD)
            r = await call(session, 'discord_read_channel', account='main', channel_id=fd.C_THREAD)
            check('read: thread after rate limit retry', r.get('messages', [{}])[0].get('text') == '土曜はどう？'
                  and r['channel']['thread_of']['name'] == '雑談', r)
            r = await call(session, 'discord_read_channel', account='main', channel_id='雑談')
            check('read: name instead of id', 'チャンネル ID' in r.get('error', ''), r)
            r = await call(session, 'discord_get_message', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000002')
            m = r['message']
            check('get message: full embed + attachment url', m['embeds'][0]['fields'] == [{'name': 'a', 'value': 'b'}]
                  and m['attachments'][0]['url'].endswith('/cdn/memo.txt'), m)
            r = await call(session, 'discord_get_message', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000999')
            check('get message: unknown hint', '10008' in r.get('error', '') and 'message_id' in r['error'], r)

            # 検索・ピン
            r = await call(session, 'discord_search_messages', account='main', server=G1, content='見て')
            check('search: hit with channel name', r['total'] == 1 and r['messages'][0]['channel']['name'] == '雑談'
                  and r['messages'][0]['url'].endswith('1300000000000000002'), r)
            r = await call(session, 'discord_search_messages', account='main', server=G1, author_id=['800000000000000001'])
            check('search: thread hit named', {m['channel']['name'] for m in r['messages']} == {'雑談', '週末の予定'}, r)
            r = await call(session, 'discord_search_messages', account='main', server=G1)
            check('search: needs condition', '条件' in r.get('error', ''), r)
            r = await call(session, 'discord_list_pins', account='main', channel_id=fd.C_GENERAL)
            check('pins', r['pins'][0]['id'] == '1300000000000000002' and r['pins'][0]['pinned_at'].endswith('+09:00'), r)

            # ユーザー
            r = await call(session, 'discord_search_members', account='main', server=G1, query='ta')
            check('members: search', r['members'][0]['name'] == 'たろう' and r['members'][0]['roles'] == ['管理人'], r)
            r = await call(session, 'discord_search_members', account='main', server=G1, limit=1)
            check('members: list with next_after', len(r['members']) == 1 and r['next_after'] == '800000000000000001', r)
            r = await call(session, 'discord_read_user_profile', account='main', user_id='800000000000000001', server=G1)
            check('profile: member info', r['name'] == '太郎' and r['member']['nick'] == 'たろう' and r['member']['roles'] == ['管理人'], r)
            r = await call(session, 'discord_read_user_profile', account='main', user_id='800000000000000002', server=G2)
            check('profile: not a member', r['member'] is None and 'メンバーではない' in r['note'], r)
            r = await call(session, 'discord_read_user_profile', account='main')
            check('profile: self', r['username'] == 'helper-a' and r['bot'] is True, r)

            # 送る
            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_GENERAL,
                           content='@everyone 了解 <@800000000000000001>', reply_to='1300000000000000002')
            sent = fd.state['sent'][-1]['body']
            check('send: ok', r.get('ok') and len(r['message_ids']) == 1, r)
            check('send: no everyone ping', sent['allowed_mentions'] == {'parse': ['users'], 'replied_user': True}, sent)
            check('send: reply reference', sent['message_reference']['message_id'] == '1300000000000000002', sent)
            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_GENERAL,
                           content='@here 全員へ', notify_everyone=True, suppress_embeds=True)
            sent = fd.state['sent'][-1]['body']
            check('send: everyone allowed when asked', 'everyone' in sent['allowed_mentions']['parse'] and sent['flags'] == 4, sent)
            long_text = '\n'.join(f'{i:04d} ' + 'あ' * 90 for i in range(50))  # 約 4800 文字
            before = len(fd.state['sent'])
            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_GENERAL, content=long_text)
            parts = [s['body']['content'] for s in fd.state['sent'][before:]]
            check('send: long text split', r.get('parts') == 3 and all(len(p) <= 2000 for p in parts)
                  and '\n'.join(parts) == long_text, r)
            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_GENERAL, content='x' * 20000)
            check('send: too long rejected', '長すぎる' in r.get('error', ''), r)
            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_VOICE, content='hi')
            check('send: voice channel hint', '50008' in r.get('error', ''), r)
            r = await call(session, 'discord_send_message', account='sub', channel_id=fd.C_OTHER, content='hi')
            check('send: read_only refused', '読み取り専用' in r.get('error', ''), r)
            r = await call(session, 'discord_send_dm', account='main', user_id='800000000000000001', content='DM です')
            check('dm: sent', r.get('channel_id') == fd.C_DM and fd.state['sent'][-1]['channel'] == fd.C_DM, r)
            r = await call(session, 'discord_read_channel', account='main', channel_id=fd.C_DM)
            check('dm: read back', r['messages'][-1]['text'] == 'DM です' and r['channel']['type'] == 'dm'
                  and r['messages'][-1]['url'].startswith('https://discord.com/channels/@me/'), r)

            # 編集・削除
            r = await call(session, 'discord_edit_message', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000004', content='直した')
            check('edit own', r.get('ok') and r['edited'], r)
            r = await call(session, 'discord_edit_message', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000001', content='x')
            check('edit others refused', 'ボット自身' in r.get('error', '') and '1300000000000000001' not in fd.state['edited'], r)
            r = await call(session, 'discord_delete_message', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000002')
            check('delete others refused', 'ボット自身' in r.get('error', ''), r)
            r = await call(session, 'discord_delete_message', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000004')
            check('delete own', r.get('ok') and fd.state['deleted'] == ['1300000000000000004'], r)

            # リアクション
            r = await call(session, 'discord_add_reaction', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000001', emoji=':party:')
            check('reaction: custom by name', r.get('emoji') == 'party:400000000000000001' and fd.state['reactions'][-1][0] == 'PUT', r)
            r = await call(session, 'discord_add_reaction', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000001', emoji='👍', remove=True)
            check('reaction: unicode remove', r.get('removed') and fd.state['reactions'][-1] == ('DELETE', '1300000000000000001', '👍'), r)
            r = await call(session, 'discord_add_reaction', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000001', emoji='nope')
            check('reaction: unknown custom', 'nope' in r.get('error', ''), r)
            r = await call(session, 'discord_get_reactions', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000002')
            check('reactions: users', [x['emoji'] for x in r['reactions']] == ['👍', ':party:'] and len(r['reactions'][0]['users']) == 2, r)
            r = await call(session, 'discord_get_reactions', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000002', emoji='party')
            check('reactions: filter', [x['emoji'] for x in r['reactions']] == [':party:'], r)

            # スレッド
            r = await call(session, 'discord_create_thread', account='main', channel_id=fd.C_GENERAL, name='相談', message_id='1300000000000000001')
            check('thread: from message', r.get('ok') and fd.state['threads'][-1].get('message') == '1300000000000000001', r)
            r = await call(session, 'discord_create_thread', account='main', channel_id=fd.C_FORUM, name='質問です')
            check('thread: forum needs content', 'content' in r.get('error', ''), r)
            r = await call(session, 'discord_create_thread', account='main', channel_id=fd.C_FORUM, name='質問です', content='教えて')
            check('thread: forum post', r.get('ok') and fd.state['threads'][-1]['body']['message']['content'] == '教えて'
                  and 'message_ids' not in r, r)
            r = await call(session, 'discord_create_thread', account='main', channel_id=fd.C_GENERAL, name='単独', content='最初')
            check('thread: standalone + first message', r.get('ok') and fd.state['threads'][-1]['body']['type'] == 11
                  and fd.state['sent'][-1]['channel'] == r['thread_id'], r)

            # ファイル
            r = await call(session, 'discord_read_file', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000002')
            check('file: several need id', 'attachment_id' in r.get('error', ''), r)
            r = await call(session, 'discord_read_file', account='main', channel_id=fd.C_GENERAL, message_id='1300000000000000002',
                           attachment_id='500000000000000001')
            check('file: text', r.get('content') == 'メモの中身です\n', r)
            res = await call(session, 'discord_read_file', raw=True, account='main', channel_id=fd.C_GENERAL,
                             message_id='1300000000000000002', attachment_id='500000000000000002')
            check('file: image', any(isinstance(c, ImageContent) and c.mime_type == 'image/png' for c in res), res)
            r = await call(session, 'discord_upload_file', account='main', channel_id=fd.C_GENERAL, filename='a.txt',
                           content_text='中身', message='ファイルです')
            up = fd.state['sent'][-1]
            check('upload: multipart', r.get('ok') and up['files'][0]['data'] == '中身'.encode() and up['files'][0]['field'] == 'files[0]'
                  and up['body']['content'] == 'ファイルです' and up['body']['attachments'][0]['filename'] == 'a.txt', up)
            r = await call(session, 'discord_upload_file', account='main', channel_id=fd.C_GENERAL, filename='b.bin',
                           content_base64=base64.b64encode(b'\x00\x01').decode())
            check('upload: base64', r.get('ok') and fd.state['sent'][-1]['files'][0]['data'] == b'\x00\x01', r)
            r = await call(session, 'discord_upload_file', account='main', channel_id=fd.C_GENERAL, filename='c.bin', content_base64='!!!')
            check('upload: bad base64', 'Base64' in r.get('error', ''), r)

            r = await call(session, 'discord_list_emojis', account='main', server=G1, query='par')
            check('emojis', r['emojis'] == [{'name': 'party', 'id': '400000000000000001'}], r)

            # 別のボットからは見えない・扱うサーバーの制限
            r = await call(session, 'discord_read_channel', account='sub', channel_id=fd.C_GENERAL)
            check('other bot cannot see', '10003' in r.get('error', ''), r)
            (tmp / 'accounts' / 'limited.toml').write_text(f'bot_token = "{fd.TOKEN_A}"\nservers = ["{G2}"]\n', encoding='utf-8')
            r = await call(session, 'discord_list_servers', account='limited')
            check('limit: servers filtered', [s['name'] for s in r['servers']] == ['Work Lab'], r)
            r = await call(session, 'discord_read_channel', account='limited', channel_id=fd.C_GENERAL)
            check('limit: channel refused', 'servers' in r.get('error', ''), r)
            r = await call(session, 'discord_list_channels', account='limited')
            check('limit: single server default', r['server']['name'] == 'Work Lab', r)
            r = await call(session, 'discord_list_channels', account='limited', server=G1)
            check('limit: explicit server refused', '扱わない' in r.get('error', ''), r)

            # 認証失敗のヒント
            (tmp / 'accounts' / 'bad.toml').write_text('bot_token = "BADTOKENXXXXXXXXXXX.abcdef.xxxxxxxxxxxxxxxxxxxxxx"\n', encoding='utf-8')
            r = await call(session, 'discord_list_servers', account='bad')
            check('invalid token hint', '401' in r.get('error', '') and 'bot_token' in r['error'], r)
            (tmp / 'accounts' / 'broken.toml').write_text('bot_token = "xoxp-not-discord"\n', encoding='utf-8')
            r = await call(session, 'discord_list_accounts')
            acc = {a['name']: a for a in r['accounts']}
            check('accounts: errors per account', 'error' in acc['bad'] and 'bot_token' in acc['broken']['error'], r)

    await webhook_tests()
    await legacy_tests()
    await viewer_tests()

    # 間違ったコネクタ URL は 404
    import httpx
    async with httpx.AsyncClient() as c:
        res = await c.post(f'http://127.0.0.1:{MCP_PORT}/mcp/{"cd" * 32}', json={})
        check('wrong token -> 404', res.status_code == 404, res.status_code)
        res = await c.post(f'http://127.0.0.1:{MCP_PORT}/mcp', json={})
        check('no token -> 404', res.status_code == 404, res.status_code)


async def webhook_tests():
    """claude: webhook で AI の名前を付けて投稿する"""
    async with streamable_http_client(f'http://127.0.0.1:{MCP_PORT}/mcp/{TOKEN_CLAUDE}') as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await call(session, 'discord_list_accounts')
            check('webhook: client info', r['client'] == {**r['client'], 'id': 'claude', 'post_as': 'webhook', 'name': 'Claude'}, r['client'])

            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_GENERAL, content='やあ',
                           reply_to='1300000000000000001')
            sent = fd.state['sent'][-1]
            created = fd.WEBHOOKS[fd.C_GENERAL][0]
            check('webhook: own name has no "discord"', 'discord' not in (created.get('name') or '').lower(), created)
            check('webhook: sent via webhook', r.get('ok') and sent.get('via') == 'webhook' and 'Claude' in r['posted_as'], r)
            check('webhook: name + avatar', sent['body']['username'] == 'Claude'
                  and sent['body']['avatar_url'] == 'https://example.com/claude.png', sent)
            check('webhook: reply as link', sent['body']['content'] ==
                  f'-# ↪ 返信: https://discord.com/channels/{G1}/{fd.C_GENERAL}/1300000000000000001\nやあ'
                  and 'message_reference' not in sent['body'], sent['body'])
            check('webhook: created once', fd.state['webhooks_created'] == [fd.C_GENERAL], fd.state['webhooks_created'])
            hook_msg = r['message_ids'][0]
            await call(session, 'discord_send_message', account='main', channel_id=fd.C_GENERAL, content='2 通目')
            check('webhook: reused', fd.state['webhooks_created'] == [fd.C_GENERAL], fd.state['webhooks_created'])

            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_THREAD, content='スレッドへ')
            sent = fd.state['sent'][-1]
            check('webhook: thread via parent hook', r.get('ok') and sent['channel'] == fd.C_THREAD and sent.get('via') == 'webhook'
                  and fd.state['webhooks_created'] == [fd.C_GENERAL], sent)
            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_WORK, content='x')
            check('webhook: missing permission hint', 'ウェブフックの管理' in r.get('error', ''), r)

            r = await call(session, 'discord_read_channel', account='main', channel_id=fd.C_GENERAL, limit=5)
            mine = next(m for m in r['messages'] if m['id'] == hook_msg)
            check('webhook: message shows AI name', mine['author']['name'] == 'Claude', mine)
            r = await call(session, 'discord_edit_message', account='main', channel_id=fd.C_GENERAL, message_id=hook_msg, content='直した')
            c = last_call('PATCH', '/webhooks/')
            check('webhook: edit own via webhook', r.get('ok') and c and hook_msg in c[1], r)
            r = await call(session, 'discord_delete_message', account='main', channel_id=fd.C_GENERAL, message_id=hook_msg)
            check('webhook: delete own via webhook', r.get('ok') and hook_msg in fd.state['deleted'], r)

            r = await call(session, 'discord_send_dm', account='main', user_id='800000000000000001', content='DM')
            check('webhook: DM falls back to bot', r.get('ok') and fd.state['sent'][-1].get('via') is None
                  and r['posted_as'] == 'ボット自身', r)

            before = len(fd.state['webhooks_created'])
            r = await call(session, 'discord_create_thread', account='main', channel_id=fd.C_FORUM, name='AI の質問', content='質問です')
            sent = fd.state['sent'][-1]
            check('webhook: forum post via thread_name', r.get('ok') and sent['body'].get('thread_name') == 'AI の質問'
                  and sent['hook'] == '600000000000000001' and r['thread_id'] == sent['channel'], (r, sent))
            check('webhook: existing app hook reused', len(fd.state['webhooks_created']) == before, fd.state['webhooks_created'])
            r = await call(session, 'discord_create_thread', account='main', channel_id=fd.C_GENERAL, name='話題', content='最初の発言')
            sent = fd.state['sent'][-1]
            check('webhook: standalone thread first message', r.get('ok') and sent['channel'] == r['thread_id'] and sent.get('via') == 'webhook', sent)

            r = await call(session, 'discord_upload_file', account='main', channel_id=fd.C_GENERAL, filename='n.txt', content_text='メモ',
                           reply_to='1300000000000000001')
            sent = fd.state['sent'][-1]
            check('webhook: upload', r.get('ok') and sent.get('via') == 'webhook' and sent['files'][0]['data'] == 'メモ'.encode()
                  and sent['body']['content'].startswith('-# ↪ 返信:'), sent)

            # 本文に discord と書いてあっても送れる (断られるのは名前だけ)
            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_GENERAL, content='Discord の話')
            check('webhook: "discord" in content ok', r.get('ok') and fd.state['sent'][-1]['body']['content'] == 'Discord の話', r)

            # 誰かが webhook を消していたら、作り直して送る
            fd.WEBHOOKS[fd.C_GENERAL].clear()
            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_GENERAL, content='消えても大丈夫')
            check('webhook: recreated after deletion', r.get('ok') and fd.state['webhooks_created'].count(fd.C_GENERAL) == 2, r)


async def legacy_tests():
    """legacy: 表示名に "Discord" や ":" が入っていても、使えない部分を除いて投稿できる"""
    async with streamable_http_client(f'http://127.0.0.1:{MCP_PORT}/mcp/{TOKEN_LEGACY}') as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await call(session, 'discord_send_message', account='main', channel_id=fd.C_GENERAL, content='古い名前')
            sent = fd.state['sent'][-1]
            check('legacy name sanitized', r.get('ok') and sent['body']['username'] == 'Bot v2', (r, sent['body']))


async def viewer_tests():
    """viewer: 読み取り専用で、アカウント sub だけ"""
    async with streamable_http_client(f'http://127.0.0.1:{MCP_PORT}/mcp/{TOKEN_VIEWER}') as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await call(session, 'discord_list_accounts')
            check('viewer: only allowed accounts', [a['name'] for a in r['accounts']] == ['sub'] and r['client']['read_only'], r)
            r = await call(session, 'discord_list_servers')
            check('viewer: single account default', [s['name'] for s in r.get('servers', [])] == ['Other'], r)
            r = await call(session, 'discord_list_servers', account='main')
            check('viewer: other account refused', '使えない設定' in r.get('error', ''), r)
            r = await call(session, 'discord_read_channel', channel_id=fd.C_OTHER)
            check('viewer: can read', r.get('messages', [{}])[0].get('text') == 'other', r)
            r = await call(session, 'discord_send_message', channel_id=fd.C_OTHER, content='x')
            check('viewer: connector read_only', 'コネクタ (viewer) は読み取り専用' in r.get('error', ''), r)

    # 外からヘッダーで接続元を偽っても、URL のトークンで決まる
    import httpx
    async with httpx.AsyncClient() as c:
        res = await c.post(f'http://127.0.0.1:{MCP_PORT}/mcp/{TOKEN_VIEWER}',
                           headers={'x-discord-mcp-client': 'tester', 'accept': 'application/json, text/event-stream',
                                    'mcp-protocol-version': '2025-06-18'},
                           json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                 'params': {'name': 'discord_list_servers', 'arguments': {'account': 'main'}}})
        text = res.text
        check('spoofed client header ignored', '使えない設定' in text, text[:300])


async def main():
    discord = uvicorn.Server(uvicorn.Config(fd.app, host='127.0.0.1', port=DISCORD_PORT, log_level='warning'))
    mcp = uvicorn.Server(uvicorn.Config(server.build_app(), host='127.0.0.1', port=MCP_PORT, log_level='warning', access_log=False))
    tasks = [asyncio.create_task(discord.serve()), asyncio.create_task(mcp.serve())]
    while not (discord.started and mcp.started):
        await asyncio.sleep(0.05)
    try:
        await run_tests()
    finally:
        discord.should_exit = mcp.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
    print(f'\n{len(failures)} failures' if failures else '\nall passed')
    return 1 if failures else 0


if __name__ == '__main__':
    start = time.time()
    code = asyncio.run(main())
    print(f'({time.time() - start:.1f}s)')
    sys.exit(code)
