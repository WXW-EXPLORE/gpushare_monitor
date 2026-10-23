#!/usr/bin/env python3
"""Read-only GPUShare availability probe and SMTP alert daemon (stdlib only)."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
from email.message import EmailMessage
import fcntl
import json
import logging
import os
from pathlib import Path
import random
import re
import smtplib
import ssl
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOG = logging.getLogger('gpushare_monitor')
MAX_RESPONSE = 1024 * 1024
UNSAFE_PATH_PARTS = {'start', 'stop', 'restart', 'reboot', 'boot', 'create', 'buy', 'order', 'pay', 'charge', 'release', 'delete', 'update', 'operate', 'power', 'rent'}


class NoRedirect(HTTPRedirectHandler):
    """Prevent forwarding session headers across redirects."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class ProbeError(Exception):
    """A probe failure is UNKNOWN, never interpreted as out-of-stock."""


def path_value(root, path):
    if not isinstance(path, str) or not path.strip():
        raise ProbeError('JSON 字段路径未配置')
    value = root
    for key in path.split('.'):
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ProbeError(f'响应缺少字段：{path}')
    return value


def validate(cfg):
    target, source = cfg['target'], cfg['source']
    if not str(target.get('machine_id', '')).strip():
        raise ValueError('请填写 target.machine_id（具体机器 ID，不是 GPU 型号）')
    if type(target.get('gpu_count')) is not int or target['gpu_count'] < 1:
        raise ValueError('target.gpu_count 必须是正整数')
    if not source.get('confirmed_read_only'):
        raise ValueError('仅在确认 endpoint 是只读查询后，将 source.confirmed_read_only 设为 true')
    url = source.get('url', '')
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in {'gpushare.com', 'www.gpushare.com', 'api.gpushare.com'}:
        raise ValueError('只允许 HTTPS 的 gpushare.com 官方域名，不允许其他站点')
    if parts.username or parts.password or parts.fragment or parts.port not in (None, 443):
        raise ValueError('URL 不允许包含用户密码、片段或非默认端口')
    segments = {p.lower().replace('_', '-') for p in parts.path.split('/') if p}
    if any(p in UNSAFE_PATH_PARTS for p in segments):
        raise ValueError('拒绝访问疑似修改实例状态的接口')
    if source.get('method', 'GET') not in ('GET', 'POST'):
        raise ValueError('仅支持只读 GET 或已确认安全的 POST 查询')
    for field in ('records_path', 'id_path', 'free_path'):
        if not source.get(field):
            raise ValueError(f'请配置 source.{field}')
    interval = cfg.get('poll_seconds', 60)
    if type(interval) is not int or interval < 60:
        raise ValueError('poll_seconds 至少 60 秒')


def env_headers(src):
    headers = {'Accept': 'application/json', 'User-Agent': 'gpushare-monitor/1.0 (read-only)'}
    for key, value in src.get('headers', {}).items():
        if not isinstance(value, str) or not value.startswith('env:'):
            raise ProbeError('自定义请求头只能引用 env:变量名，不允许在配置文件存储凭据')
        name = value[4:]
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ProbeError('非法的环境变量名')
        secret = os.environ.get(name)
        if not secret:
            raise ProbeError(f'请求所需环境变量未配置：{name}')
        headers[key] = secret
    return headers


def fetch_json(src):
    headers = env_headers(src)
    method = src.get('method', 'GET')
    data = None
    if method == 'POST':
        data = json.dumps(src.get('json_body', {}), ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    request = Request(src['url'], data=data, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(request, timeout=12) as response:
            body = response.read(MAX_RESPONSE + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ProbeError('接口重定向：可能需要网页登录；为防止 Cookie 泄漏，拒绝自动跳转') from exc
        if exc.code in (401, 403):
            raise ProbeError('登录态失效或无访问权限') from exc
        if exc.code == 429:
            raise ProbeError('平台限流（HTTP 429）') from exc
        raise ProbeError(f'接口 HTTP {exc.code}') from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProbeError(f'网络访问失败：{type(exc).__name__}') from exc
    if len(body) > MAX_RESPONSE:
        raise ProbeError('响应体异常过大')
    try:
        return json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise ProbeError('接口返回非 JSON 数据（可能需要登录）') from exc


def evaluate(data, cfg):
    """Return (available: bool, free: int). Unknown results raise ProbeError."""
    src, target = cfg['source'], cfg['target']
    if src.get('success_path'):
        if path_value(data, src['success_path']) != src.get('success_value', True):
            raise ProbeError('接口明确返回失败或未登录')
    items = path_value(data, src['records_path'])
    if not isinstance(items, list):
        raise ProbeError('机器列表不是数组，无法判断目标机器')
    matches = []
    for item in items:
        try:
            ident = path_value(item, src['id_path'])
        except ProbeError:
            raise ProbeError('列表缺少机器 ID，不能安全匹配') from None
        if str(ident) == str(target['machine_id']):
            matches.append(item)
    if len(matches) != 1:
        raise ProbeError('目标机器缺失或机器 ID 非唯一，不能确认库存')
    item = matches[0]
    if src.get('model_path'):
        if str(path_value(item, src['model_path'])) != str(target.get('gpu_model', '')):
            raise ProbeError('显卡型号与配置不一致')
    count = path_value(item, src['free_path'])
    if isinstance(count, bool) or not (type(count) is int and count >= 0):
        raise ProbeError('空闲显卡数不是非负整数，不能确认库存')
    return count >= target['gpu_count'], count


def inspect_json(data, machine_id):
    """Show only structural hints, never sample values or confidential fields."""
    matches = []
    visited = 0

    def walk(obj, path='', depth=0):
        nonlocal visited
        visited += 1
        if visited > 5000 or depth > 12 or len(matches) >= 10:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if not isinstance(key, str):
                    continue
                if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) == machine_id:
                    candidates = sorted(k for k, val in obj.items()
                        if isinstance(k, str) and isinstance(val, int) and not isinstance(val, bool)
                        and any(word in k.lower() for word in ('free', 'avail', 'idle', 'stock', 'remain', 'left')))
                    matches.append((path or '(root)', key, candidates))
            for key, value in obj.items():
                if isinstance(key, str) and isinstance(value, (list, dict)):
                    walk(value, f'{path}.{key}' if path else key, depth+1)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj[:300]):
                if isinstance(item, (list, dict)):
                    walk(item, f'{path}.{idx}' if path else str(idx), depth+1)

    walk(data)
    if not matches:
        return ('没有找到与目标 machine_id 完全一致的机器记录。'
                '可能是接口不包含目标主机、需要登录或字段不是直接字符串，不能作为库存来源。')
    lines = [f'发现 {len(matches)} 个可能的机器 ID 对应记录（只显示字段名，不显示数据）：']
    for path, key, candidates in matches:
        lines.append(f'路径: {path}  ID字段: {key}  候选库存字段名: {candidates or "未发现"}')
    lines.append('注意：候选字段须人工核对业务含义；仅有记录不代表可开机，不能自动启用监控。')
    return '\n'.join(lines)


def smtp_config():
    keys = ('SMTP_HOST', 'SMTP_FROM', 'SMTP_TO')
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        raise RuntimeError('邮件参数未填写：' + ', '.join(missing))
    return {k: os.environ.get(k, '') for k in keys}


def send_mail(subject, body):
    m = smtp_config()
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = m['SMTP_FROM']
    msg['To'] = m['SMTP_TO']
    msg.set_content(body)
    user, password = os.environ.get('SMTP_USER', ''), os.environ.get('SMTP_PASSWORD', '')
    if bool(user) != bool(password):
        raise RuntimeError('SMTP_USER 与 SMTP_PASSWORD 必须同时设置')
    host = m['SMTP_HOST']
    port = int(os.environ.get('SMTP_PORT', '465'))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)


def load_state(path):
    try:
        obj = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(obj, dict):
            raise ValueError('invalid state')
        return obj
    except FileNotFoundError:
        return {}


def save_state(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def cycle(cfg, state, probe=fetch_json, notify=send_mail, now=None):
    now = now or datetime.now(timezone.utc).isoformat(timespec='seconds')
    result = dict(state)
    # Bind state to a particular resource; do not inherit alert suppression from other machines.
    signature = f"{cfg['target']['machine_id']}:{cfg['target']['gpu_count']}:{cfg['source']['url']}"
    if result.get('signature') != signature:
        result = {'signature': signature, 'alerted': False, 'unavailable_streak': 0, 'errors': 0}
    try:
        data = probe(cfg['source'])
        available, free = evaluate(data, cfg)
    except (ProbeError, ValueError) as exc:
        result['errors'] = result.get('errors', 0) + 1
        result['unavailable_streak'] = 0
        result['last_status'] = 'unknown'
        result['last_check'] = now
        return result, f'UNKNOWN: {exc}'
    result['errors'] = 0
    result['last_check'] = now
    result['free_gpus'] = free
    if available:
        result['unavailable_streak'] = 0
        result['last_status'] = 'available'
        if not result.get('alerted'):
            target = cfg['target']
            message = (f"GPUShare 发现候选空闲 GPU！\n\n"
                       f"目标机器：{target['machine_id']}\n"
                       f"GPU：{target.get('gpu_model', '未配置')}，需要 {target['gpu_count']} 张\n"
                       f"查询返回空闲数量：{free}\n"
                       f"查询时间（UTC）：{now}\n\n"
                       f"请自行登录并尝试开机：{target.get('console_url', 'https://gpushare.com/')}\n\n"
                       "仅依据配置的只读数据源判断，库存瞬时变化，不保证开机成功。")
            notify('【GPUShare】目标机器出现空闲 GPU，请尽快手动开机', message)
            result['alerted'] = True
            result['last_alert'] = now
            return result, 'AVAILABLE: mail sent'
        return result, 'AVAILABLE: already notified'
    result['last_status'] = 'unavailable'
    result['unavailable_streak'] = result.get('unavailable_streak', 0) + 1
    if result['unavailable_streak'] >= 2:
        result['alerted'] = False
    return result, f'UNAVAILABLE: {free} free'


def health_mail_if_needed(state, notify=send_mail):
    if state.get('errors', 0) < 5:
        return state
    now = time.time()
    if now - state.get('last_error_alert_epoch', 0) < 6 * 3600:
        return state
    notify('【GPUShare】监测连续失败，需要检查',
           'GPUShare 库存检测连续失败至少 5 次。请检查网络、接口格式、登录态及服务日志。\n'
           '监控失败时不会发送“有卡”通知。')
    state['last_error_alert_epoch'] = now
    return state


@contextlib.contextmanager
def process_lock(state_path):
    lock = str(state_path) + '.lock'
    Path(lock).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(lock, 'a+', encoding='utf-8') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('已有一个监控进程在运行') from None
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description='GPUShare 只读候补监测及邮件通知')
    parser.add_argument('command', choices=['check', 'once', 'monitor', 'test-mail', 'inspect-json'])
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--state', default='state/state.json')
    parser.add_argument('--sample-file', help='离线 JSON 样本，仅用于 inspect-json；不联网、不发邮件')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'test-mail':
        send_mail('【GPUShare】监控邮件测试', '测试成功。此邮件不代表发现空闲显卡。')
        LOG.info('邮件测试发送成功')
        return 0
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8'))
    if args.command == 'inspect-json':
        if not args.sample_file:
            raise ValueError('inspect-json 需要 --sample-file 文件路径')
        sample_path = Path(args.sample_file)
        if sample_path.stat().st_size > MAX_RESPONSE:
            raise ValueError('样本文件超过 1 MiB，不予处理')
        data = json.loads(sample_path.read_text(encoding='utf-8'))
        print(inspect_json(data, str(cfg['target']['machine_id'])))
        return 0
    validate(cfg)
    if args.command == 'check':
        avail, free = evaluate(fetch_json(cfg['source']), cfg)
        print(f"{'AVAILABLE' if avail else 'UNAVAILABLE'} free={free} target={cfg['target']['machine_id']}")
        return 0
    with process_lock(args.state):
        while True:
            state = load_state(args.state)
            try:
                state, msg = cycle(cfg, state)
                try:
                    state = health_mail_if_needed(state)
                except (OSError, smtplib.SMTPException, RuntimeError) as exc:
                    LOG.error('健康告警邮件发送失败：%s', type(exc).__name__)
            except (OSError, smtplib.SMTPException, RuntimeError) as exc:
                # In particular, do not mark availability as alerted if email failed.
                LOG.error('邮件发送失败，将在下次检测时重试：%s', type(exc).__name__)
                state['last_status'] = 'notification_failed'
                msg = 'NOTIFICATION_FAILED'
            save_state(args.state, state)
            LOG.info('%s', msg)
            if args.command == 'once':
                return 1 if msg.startswith('UNKNOWN') or msg == 'NOTIFICATION_FAILED' else 0
            delay = cfg.get('poll_seconds', 60) + random.uniform(0, 10)
            if msg.startswith('UNKNOWN'):
                delay = max(delay, min(900, 60 * (2 ** min(state.get('errors', 1)-1, 4))))
            time.sleep(delay)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, ProbeError, RuntimeError, OSError, json.JSONDecodeError) as error:
        LOG.error('%s', error)
        sys.exit(2)
