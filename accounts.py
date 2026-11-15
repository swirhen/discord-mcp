"""Discord アカウント (ボットごとのトークン) の設定の読み込み。

アカウントごとに accounts/<アカウント名>.toml が 1 つ (Git 管理外。トークンを含む)。
1 つのボットで、招待したサーバー全部を扱える。ボットを分けたいとき (読み取り専用にするなど) はファイルを増やす。
ファイルが変わったら読み直すので、追加・変更に再起動は要らない。

環境変数
  DISCORD_MCP_ACCOUNTS_DIR  設定ファイルの置き場所 (省略時はリポジトリ直下の accounts/)
"""
from pathlib import Path
import datetime
import json
import os
import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # 本番は Ubuntu 22.04 の Python 3.10
    import tomli as tomllib

ACCOUNTS_DIR = Path(os.environ.get('DISCORD_MCP_ACCOUNTS_DIR', Path(__file__).resolve().parent / 'accounts'))
NAME_RE = re.compile(r'^[a-z0-9_-]{1,32}$')
TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{10,}$')
SNOWFLAKE_RE = re.compile(r'^\d{15,21}$')

_cache: dict[str, tuple[tuple, dict]] = {}


class AccountError(ValueError):
    pass


def account_path(name: str) -> Path:
    if not NAME_RE.fullmatch(name or ''):
        raise AccountError(f'アカウント名「{name}」の形が違う (英小文字・数字・ハイフン・アンダースコアで 32 文字まで)')
    return ACCOUNTS_DIR / f'{name}.toml'


def list_accounts() -> list[str]:
    """設定ファイルのあるアカウント名を名前順で返す"""
    try:
        names = [p.stem for p in ACCOUNTS_DIR.glob('*.toml') if NAME_RE.fullmatch(p.stem)]
    except OSError:
        return []
    return sorted(names)


def read_account(name: str) -> dict:
    """アカウントの設定を返す ({'name', 'bot_token', 'label', 'read_only', 'servers'})。ファイルが変わったときだけ読み直す。
    servers は扱ってよいサーバー ID の集合 (空ならボットが入っているサーバー全部)"""
    path = account_path(name)
    try:
        st = path.stat()
    except FileNotFoundError:
        raise AccountError(f'アカウント「{name}」の設定がない ({path})。discord_list_accounts で名前を確かめる') from None
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    cached = _cache.get(name)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise AccountError(f'アカウント「{name}」の設定を読めない ({e})') from e
    token = str(data.get('bot_token', '')).strip()
    if not TOKEN_RE.fullmatch(token):
        raise AccountError(f'アカウント「{name}」の bot_token が未設定か形が違う (Developer Portal の Bot ページで Reset Token して得たトークンを書く)')
    servers = data.get('servers', []) or []
    if not isinstance(servers, list) or not all(SNOWFLAKE_RE.fullmatch(str(s)) for s in servers):
        raise AccountError(f'アカウント「{name}」の servers はサーバー ID (数字) の配列で書く')
    config = {
        'name': name,
        'bot_token': token,
        'label': str(data.get('label', '') or ''),
        'read_only': bool(data.get('read_only', False)),
        'servers': frozenset(str(s) for s in servers),
    }
    _cache[name] = (key, config)
    return config


# ---- 書き込み (管理画面から使う。値が正しくなければ AccountError) ----
LABEL_MAX = 200


def _toml_string(value: str) -> str:
    # JSON の文字列の書き方は TOML の基本文字列としてもそのまま読める (\" \ \n \uXXXX)
    return json.dumps(value, ensure_ascii=False)


def write_account(name: str, bot_token: str, label: str = '', read_only: bool = False,
                  servers: list[str] | None = None, create: bool = False) -> dict:
    """accounts/<name>.toml を書く (600)。create=True ならすでにあるとき、False ならないときに失敗する"""
    path = account_path(name)
    if create and path.exists():
        raise AccountError(f'アカウント「{name}」はすでにある')
    if not create and not path.exists():
        raise AccountError(f'アカウント「{name}」がない')
    bot_token = (bot_token or '').strip()
    label = (label or '').strip()
    servers = [str(s).strip() for s in servers or [] if str(s).strip()]
    if not TOKEN_RE.fullmatch(bot_token):
        raise AccountError('bot_token は Developer Portal の Bot ページで Reset Token して得たトークン (点で区切った 3 つの部分)')
    if len(label) > LABEL_MAX or '\n' in label:
        raise AccountError(f'説明は 1 行・{LABEL_MAX} 文字まで')
    if not all(SNOWFLAKE_RE.fullmatch(s) for s in servers):
        raise AccountError('扱うサーバーはサーバー ID (数字) で指定する')
    lines = ['# discord-mcp のアカウント設定 (管理画面で保存)', f'bot_token = {_toml_string(bot_token)}',
             f'label = {_toml_string(label)}', f'read_only = {"true" if read_only else "false"}',
             'servers = [' + ', '.join(_toml_string(s) for s in sorted(set(servers))) + ']']
    ACCOUNTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(temporary, path)
    _cache.pop(name, None)
    return read_account(name)


def delete_account(name: str) -> Path:
    """設定を消さずに accounts/.deleted/<name>-<日時>.toml へ移す。移した先を返す"""
    path = account_path(name)
    if not path.exists():
        raise AccountError(f'アカウント「{name}」がない')
    trash = ACCOUNTS_DIR / '.deleted'
    trash.mkdir(mode=0o700, exist_ok=True)
    dest = trash / f'{name}-{datetime.datetime.now().strftime("%Y%m%d-%H%M%S")}.toml'
    os.replace(path, dest)
    _cache.pop(name, None)
    return dest
