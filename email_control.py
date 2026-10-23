"""Optional mail-to-email control channel, without a web server."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
import hashlib
import hmac
import imaplib
import json
import os
import re
import secrets
from urllib.parse import urlencode, quote

TOKEN_TTL_SECONDS = 48 * 3600
MAX_UIDS_PER_POLL = 50
CONTROL_PATTERN = re.compile(r'^GPUCTRL (PAUSE|RESUME) ([A-Za-z0-9_.-]{15,1200})$')


class MailControlError(Exception):
    pass


def read_control_config(environ=None):
    env = environ if environ is not None else os.environ
    keys = ('IMAP_HOST', 'IMAP_USER', 'IMAP_PASSWORD',
            'CONTROL_AUTHORIZED_FROM', 'CONTROL_SECRET')
    supplied = [k for k in keys if env.get(k)]
    if not supplied:
        return None
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise MailControlError('邮件控制缺少本地配置：' + ', '.join(missing))
    secret = env['CONTROL_SECRET']
    if len(secret.encode('utf-8')) < 32:
        raise MailControlError('CONTROL_SECRET 不得少于 32 字节，请用 secrets.token_urlsafe(32) 生成')
    receiver = parseaddr(env['IMAP_USER'])[1].lower()
    sender = parseaddr(env['CONTROL_AUTHORIZED_FROM'])[1].lower()
    if not receiver or not sender or receiver != env['IMAP_USER'].lower().strip() or sender != env['CONTROL_AUTHORIZED_FROM'].lower().strip():
        raise MailControlError('IMAP_USER / CONTROL_AUTHORIZED_FROM 必须是单个完整邮箱地址')
    try:
        port = int(env.get('IMAP_PORT', '993'))
    except ValueError as exc:
        raise MailControlError('IMAP_PORT 不合法') from exc
    if port < 1 or port > 65535:
        raise MailControlError('IMAP_PORT 不合法')
    return {'host': env['IMAP_HOST'], 'port': port, 'user': receiver,
            'password': env['IMAP_PASSWORD'], 'sender': sender,
            'secret': secret.encode('utf-8')}


def make_token(action, secret, now=None):
    if action not in ('pause', 'resume'):
        raise ValueError('Unsupported action')
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    payload = {'a': action, 'e': now + TOKEN_TTL_SECONDS, 'n': secrets.token_urlsafe(12), 'v': 1}
    raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
    encoded = base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
    signed = hmac.new(secret, encoded.encode('ascii'), hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(signed).rstrip(b'=').decode('ascii')
    return encoded + '.' + signature


def verify_token(action, token, secret, now=None):
    if action not in ('pause', 'resume') or not isinstance(token, str) or len(token) > 1200:
        return False
    try:
        payload64, sig64 = token.split('.')
        expected = hmac.new(secret, payload64.encode('ascii'), hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(sig64 + '=' * (-len(sig64) % 4))
        if not hmac.compare_digest(expected, actual):
            return False
        raw = base64.urlsafe_b64decode(payload64 + '=' * (-len(payload64) % 4))
        data = json.loads(raw)
        now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
        return (data.get('v') == 1 and data.get('a') == action
                and type(data.get('e')) is int and now <= data['e'] <= now + TOKEN_TTL_SECONDS
                and isinstance(data.get('n'), str) and len(data['n']) >= 12)
    except (TypeError, ValueError, UnicodeError, KeyError, json.JSONDecodeError):
        return False


def mailto_link(action, cfg, now=None):
    token = make_token(action, cfg['secret'], now)
    subject = f'GPUCTRL {action.upper()} {token}'
    return 'mailto:' + quote(cfg['user'], safe='@') + '?' + urlencode({
        'subject': subject,
        'body': 'GPUShare 监控控制指令：请保持邮件主题不变，点击发送即可。\n'
                '这是对你自己监控程序的控制，不会启动或购买 GPU。'
    })


def control_section(action, cfg):
    label = '暂停监测' if action == 'pause' else '恢复监测'
    return ('\n\n【邮件控制】\n'
            f'点击以下链接 → 邮件应用打开预填邮件 → 由你点击“发送”，即可{label}：\n'
            f'{mailto_link(action, cfg)}\n'
            '指令在 48 小时内有效；服务器通常在下一次轮询（约 60 秒）处理。\n'
            '不要转发含控制链接的邮件。')


def parse_signed_command(raw, cfg, now=None):
    if len(raw) > 64 * 1024:
        return None
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        if parseaddr(str(msg.get('From', '')))[1].lower() != cfg['sender']:
            return None
        subject = str(msg.get('Subject', '')).strip()
        match = CONTROL_PATTERN.fullmatch(subject)
        if not match:
            return None
        action = match.group(1).lower()
        return (action, match.group(2)) if verify_token(action, match.group(2), cfg['secret'], now=now) else None
    except (ValueError, TypeError, UnicodeError):
        return None


def parse_control_message(raw, cfg, now=None):
    command = parse_signed_command(raw, cfg, now=now)
    return command[0] if command else None


def _mailbox_uids(client, after_uid):
    status, data = client.uid('SEARCH', None, f'UID {after_uid + 1}:*')
    if status != 'OK' or not data:
        raise MailControlError('IMAP UID SEARCH 失败')
    result = [int(part) for part in (data[0] or b'').split() if part.isdigit()]
    return [uid for uid in result if uid > after_uid]


def _max_uid(client):
    status, data = client.uid('SEARCH', None, 'ALL')
    if status != 'OK' or not data:
        raise MailControlError('IMAP 初始化查询失败')
    uids = [int(part) for part in (data[0] or b'').split() if part.isdigit()]
    return max(uids, default=0)


def _uidvalidity(client):
    status, data = client.response('UIDVALIDITY')
    if status not in ('OK', 'UIDVALIDITY') or not data or not data[0]:
        raise MailControlError('无法检查 IMAP UIDVALIDITY')
    value = data[0].decode('ascii') if isinstance(data[0], bytes) else str(data[0])
    if not value.isdecimal():
        raise MailControlError('无效的 IMAP UIDVALIDITY')
    return value


def _fetch_header(client, uid):
    status, data = client.uid('FETCH', str(uid), '(BODY.PEEK[HEADER])')
    if status != 'OK' or not data:
        raise MailControlError('IMAP UID FETCH 失败')
    for part in data:
        if isinstance(part, tuple) and len(part) == 2 and isinstance(part[1], bytes):
            return part[1]
    raise MailControlError('IMAP 未返回邮件头')


def scan_mail_controls(state, cfg, client_factory=None, now=None):
    client_factory = client_factory or imaplib.IMAP4_SSL
    client = client_factory(cfg['host'], cfg['port'], timeout=15)
    try:
        client.login(cfg['user'], cfg['password'])
        status, _ = client.select('INBOX', readonly=True)
        if status != 'OK':
            raise MailControlError('IMAP INBOX 不可访问')
        validity = _uidvalidity(client)
        result = dict(state)
        if result.get('imap_uidvalidity') != validity or type(result.get('imap_last_uid')) is not int:
            result['imap_uidvalidity'] = validity
            result['imap_last_uid'] = _max_uid(client)
            return result, []
        uids = _mailbox_uids(client, result['imap_last_uid'])[:MAX_UIDS_PER_POLL]
        actions = []
        for uid in uids:
            header = _fetch_header(client, uid)
            command = parse_signed_command(header, cfg, now=now)
            if command:
                action, token = command
                fingerprint = hashlib.sha256(token.encode('ascii')).hexdigest()
                consumed = result.get('used_control_tokens', [])
                if fingerprint not in consumed:
                    result['paused'] = (action == 'pause')
                    if action == 'resume':
                        result['alerted'] = False
                    actions.append(action)
                    result['used_control_tokens'] = (consumed + [fingerprint])[-100:]
            result['imap_last_uid'] = uid
        return result, actions
    finally:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
