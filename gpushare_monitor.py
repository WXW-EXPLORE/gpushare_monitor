#!/usr/bin/env python3
"""Read-only GPUShare availability probe and SMTP alert daemon (stdlib only)."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape as html_escape
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
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from email_control import MailControlError, read_control_config, scan_mail_controls, control_section

LOG = logging.getLogger('gpushare_monitor')
MAX_RESPONSE = 1024 * 1024
UNSAFE_PATH_PARTS = {'start', 'stop', 'restart', 'reboot', 'boot', 'create', 'buy', 'order', 'pay', 'charge', 'release', 'delete', 'update', 'operate', 'power', 'rent'}


class NoRedirect(HTTPRedirectHandler):
    """Prevent redirecting login/session headers to another origin."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None



class ProbeError(Exception):
    """A probe failure is UNKNOWN, never interpreted as out-of-stock."""


class NotListed(ProbeError):
    """A complete market scan did not list the target. NOT proof of zero idle GPUs."""


class AuthenticationError(ProbeError):
    """An authentication rejection needs an immediate, credential-free alert."""


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


def target_configs(cfg):
    """Expand the primary target and additive targets into isolated queries."""
    primary = {k: v for k, v in cfg.items() if k != 'additional_targets'}
    extra = cfg.get('additional_targets', [])
    if not isinstance(extra, list) or not all(isinstance(t, dict) for t in extra):
        raise ValueError('additional_targets 必须是目标对象列表')
    configs = [primary]
    if extra and cfg['source'].get('kind') != 'gpushare_market':
        raise ValueError('多机器监控仅支持官方只读云市场接口')
    for target in extra:
        source = dict(cfg['source'])
        parts = urlsplit(source['url'])
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query['gpuName'] = target.get('gpu_model', '')
        query['pn'] = '1'
        source['url'] = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                   urlencode(query), parts.fragment))
        configs.append({**primary, 'target': dict(target), 'source': source})
    identities = []
    for item in configs:
        target = item['target']
        mode = target.get('match_mode', 'machine')
        identity = (mode, str(target.get('machine_id') if mode == 'machine'
                              else target.get('gpu_model')))
        if identity in identities:
            raise ValueError('监控目标重复，请勿重复添加同一机器或型号')
        identities.append(identity)
    return configs


def validate(cfg):
    if 'additional_targets' in cfg:
        for item in target_configs(cfg):
            validate(item)
        return
    target, source = cfg['target'], cfg['source']
    mode = target.get('match_mode', 'machine')
    if mode not in ('machine', 'gpu_model'):
        raise ValueError('target.match_mode 只能是 machine 或 gpu_model')
    if not str(target.get('gpu_model', '')).strip():
        raise ValueError('target.gpu_model 不允许为空')
    if mode == 'machine' and not str(target.get('machine_id', '')).strip():
        raise ValueError('机器精确匹配模式须填写 target.machine_id')
    if type(target.get('gpu_count')) is not int or target['gpu_count'] < 1:
        raise ValueError('target.gpu_count 必须是正整数')
    if mode == 'gpu_model' and source.get('kind') != 'gpushare_market':
        raise ValueError('按 GPU 型号匹配只支持已核验结构的 gpushare_market 数据源')
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
    if source.get('kind') == 'gpushare_market':
        validate_market_diagnostic(source)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if query.get('gpuName') != target.get('gpu_model'):
            raise ValueError('云市场 gpuName 必须与目标显卡型号一致')
        if query.get('vmType') != 'docker':
            raise ValueError('仅支持截图中验证的 vmType=docker')
        try:
            page_size = int(query.get('ps', '0'))
            min_num = int(query.get('minGpuNum', '0'))
        except ValueError as exc:
            raise ValueError('云市场分页/显卡数参数无效') from exc
        if not (1 <= page_size <= 100) or not (1 <= min_num <= target['gpu_count']):
            raise ValueError('云市场分页大小或最小显卡数配置不合法')
        max_pages = source.get('max_pages', 30)
        if type(max_pages) is not int or not (1 <= max_pages <= 60):
            raise ValueError('max_pages 必须在 1 到 60 之间')
    else:
        if source.get('kind') not in (None, 'generic'):
            raise ValueError('未知的库存数据源类型')
        if source.get('method', 'GET') not in ('GET', 'POST'):
            raise ValueError('仅支持只读 GET 或已确认安全的 POST 查询')
        for field in ('records_path', 'id_path', 'free_path'):
            if not source.get(field):
                raise ValueError(f'请配置 source.{field}')
    interval = cfg.get('poll_seconds', 30)
    if type(interval) is not int or interval < 30:
        raise ValueError('poll_seconds 至少 30 秒')


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


def fetch_one_json(src):
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
            raise AuthenticationError('登录态失效或无访问权限') from exc
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


def market_page_url(url, page):
    """Preserve every observed filter; only change the positive page number."""
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params['pn'] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(params), ''))


def validate_market_page(data):
    """Check business success and pagination structure before any inventory evaluation."""
    if isinstance(data, dict) and data.get('status') in (401, 403, '401', '403'):
        raise AuthenticationError('云市场认证失败：请更新本地 Token 或检查访问权限')
    if not isinstance(data, dict) or type(data.get('status')) is not int or data['status'] != 0:
        raise ProbeError('云市场返回非成功状态（可能未登录或登录态失效）')
    body = data.get('data')
    if not isinstance(body, dict):
        raise ProbeError('云市场响应缺少 data 对象')
    records, total = body.get('list'), body.get('total')
    if not isinstance(records, list) or type(total) is not int or total < 0:
        raise ProbeError('云市场分页数据缺失或无效')
    if not all(isinstance(record, dict) for record in records):
        raise ProbeError('云市场机器列表结构无效')
    return records, total


def fetch_market_pages(src, get_page=None):
    """Scan *all* pages or return UNKNOWN; page one alone can miss this machine.

    A market-list entry is evidence of marketplace supply, not a guaranteed
    right for an existing stopped instance to boot on that host.
    """
    get_page = get_page or fetch_one_json
    parts = urlsplit(src['url'])
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    page_size = int(params['ps'])
    first = get_page({**src, 'url': market_page_url(src['url'], 1)})
    items, total = validate_market_page(first)
    pages = (total + page_size - 1) // page_size
    if pages > src.get('max_pages', 30):
        raise ProbeError(f'目标云市场共 {total} 条，超出分页上限；不能保证遍历完整')
    combined = list(items)
    if total == 0 and items:
        raise ProbeError('云市场总数为 0 但仍有机器记录')
    for page in range(2, pages + 1):
        next_data = get_page({**src, 'url': market_page_url(src['url'], page)})
        next_items, next_total = validate_market_page(next_data)
        if next_total != total:
            raise ProbeError('分页期间市场总数发生变化，本轮重新检查')
        combined.extend(next_items)
    if len(combined) != total:
        raise ProbeError(f'分页结果不完整：预期 {total} 条，实得 {len(combined)} 条')
    return {'status': 0, 'data': {'list': combined, 'total': total}}


def fetch_json(src):
    if src.get('kind') == 'gpushare_market':
        return fetch_market_pages(src)
    return fetch_one_json(src)


def market_candidates(data, target):
    """Find *any* qualifying hosts for this GPU model, not the old machine.

    A qualifying host must individually have >= gpu_count free GPUs;
    GPU counts from separate hosts cannot be combined to meet a multi-GPU need.
    """
    items, total = validate_market_page(data)
    if len(items) != total:
        raise ProbeError('市场分页结果不完整，不能确认库存')
    mode = target.get('match_mode', 'machine')
    if mode == 'machine':
        relevant = [item for item in items if item.get('machineId') == target['machine_id']]
        if not relevant:
            raise NotListed('目标机器未出现在完整市场列表')
        if len(relevant) != 1:
            raise ProbeError('目标 machineId 出现多次，无法确认库存')
    elif mode == 'gpu_model':
        relevant = [item for item in items if item.get('gpuName') == target['gpu_model']]
        if not relevant:
            # A genuinely empty filtered listing indicates no model supply
            # observed, NOT a verified inventory of an existing VM.
            raise NotListed('完整市场列表没有此型号的机器记录')
        ids = [item.get('machineId') for item in relevant]
        if not all(isinstance(x, str) and x.strip() for x in ids):
            raise ProbeError('市场机器 ID 缺失，无法消除重复记录')
        if len(ids) != len(set(ids)):
            raise ProbeError('市场中出现重复机器 ID，拒绝重复统计')
    else:
        raise ProbeError('不支持的匹配模式')

    candidates = []
    incomplete = False
    for item in relevant:
        if item.get('gpuName') != target['gpu_model']:
            if mode == 'machine':
                raise ProbeError('machineId 匹配但 GPU 型号不一致')
            continue
        if mode == 'machine' and target.get('region') and item.get('regionName') != target['region']:
            raise ProbeError('原机器地区与预期不符')
        num, used = item.get('gpuNum'), item.get('gpuUsed')
        if (type(num) is not int or type(used) is not int or
                num < 0 or not (0 <= used <= num)):
            incomplete = True
            continue
        free = num - used
        # Ignore definitely unavailable hosts. If an indicator's *type* is
        # unknown, do not count it as in stock.
        if type(item.get('status')) is not int or type(item.get('isOnline')) is not bool:
            incomplete = True
            continue
        if item['status'] != 1 or not item['isOnline']:
            continue
        skus = item.get('skuList')
        if not isinstance(skus, list):
            incomplete = True
            continue
        payg = [sku for sku in skus if isinstance(sku, dict) and sku.get('skuName') == 'payg']
        if len(payg) != 1:
            incomplete = True
            continue
        if type(payg[0].get('status')) is not int or type(payg[0].get('enable')) is not int:
            incomplete = True
            continue
        if payg[0]['status'] != 1 or payg[0]['enable'] != 1:
            continue
        if free >= target['gpu_count']:
            candidates.append({'machine_id': item.get('machineId'),
                               'region': item.get('regionName', '未提供'),
                               'free': free})
    # One verified available host suffices for a truthful positive alert;
    # malformed *other* rows should not conceal its existence.
    if not candidates and incomplete:
        raise ProbeError('匹配型号的部分记录缺少必要字段，未能确认是否有货')
    return candidates


def evaluate_market(data, target):
    """Return (any_available, sum_free_on_individually_eligible_hosts)."""
    candidates = market_candidates(data, target)
    return bool(candidates), sum(item['free'] for item in candidates)


def evaluate(data, cfg):
    """Return (available: bool, free: int). Unknown results raise ProbeError."""
    src, target = cfg['source'], cfg['target']
    if src.get('kind') == 'gpushare_market':
        return evaluate_market(data, target)
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
    """Only show matching structure and safe field *names*, never sample contents.

    The returned hints do NOT prove the endpoint is read-only or available.
    """
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


def describe_json_shape(data, max_depth=4, max_lines=32):
    """Describe JSON shape without ever showing scalar values or access tokens.

    This can be shared safely *after* the operator checks the field names.
    No raw response content is stored or printed.
    """
    lines = []

    def describe(value, path, depth):
        if len(lines) >= max_lines:
            return
        if isinstance(value, dict):
            keys = sorted(str(k) for k in value.keys())
            lines.append(f'{path}: object keys={keys[:24]}' + (' ...' if len(keys) > 24 else ''))
            if depth < max_depth:
                for key, child in value.items():
                    if isinstance(key, str) and isinstance(child, (dict, list)):
                        describe(child, f'{path}.{key}', depth + 1)
        elif isinstance(value, list):
            lines.append(f'{path}: array length={len(value)}')
            if depth < max_depth and value:
                describe(value[0], f'{path}[0]', depth + 1)
        else:
            typename = 'null' if value is None else type(value).__name__
            lines.append(f'{path}: {typename} (value hidden)')

    describe(data, '$', 0)
    return '\n'.join(lines[:max_lines])


def validate_market_diagnostic(source):
    """Allow one screenshot-confirmed read-only GET for schema investigation.

    The endpoint's *response semantics* are not verified yet, so this must never
    enable actual alerts or write back to GPUShare.
    """
    parts = urlsplit(source.get('url', ''))
    if (parts.scheme != 'https' or parts.hostname != 'api.gpushare.com'
            or parts.port not in (None, 443) or parts.username or parts.password
            or parts.fragment or parts.path != '/app/api/market/machine/list'
            or source.get('method', 'GET') != 'GET'):
        raise ValueError('诊断模式只允许截图中的 GET https://api.gpushare.com/app/api/market/machine/list')


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
    # Render a human-readable mailto control link in modern mail clients;
    # HTML is static except for escaped server-generated mailto URLs.
    if 'mailto:' in body:
        import re as _re
        def _render_line(line):
            if line.startswith('mailto:'):
                safe_url = html_escape(line.strip(), quote=True)
                return f'<a href="{safe_url}">点此发送监控控制邮件（需你确认发送）</a>'
            return html_escape(line, quote=True)
        rendered = '<br>\n'.join(_render_line(line) for line in body.splitlines())
        msg.add_alternative('<html><body>' + rendered + '</body></html>', subtype='html')
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


def target_signature(cfg):
    t = cfg['target']
    return (f"{t.get('match_mode', 'machine')}:{t.get('machine_id', '')}:"
            f"{t['gpu_model']}:{t['gpu_count']}:{cfg['source']['url']}")


def cycle(cfg, state, probe=fetch_json, notify=send_mail, now=None):
    now = now or datetime.now(timezone.utc).isoformat(timespec='seconds')
    result = dict(state)
    signature = target_signature(cfg)
    if result.get('signature') != signature:
        # Do not discard IMAP watermark or pause state on first market cycle
        # or when changing the monitored GPU model.
        controls = {k: result[k] for k in
                    ('paused', 'imap_uidvalidity', 'imap_last_uid', 'used_control_tokens')
                    if k in result}
        result = {'signature': signature, 'alerted': False, 'unavailable_streak': 0,
                  'errors': 0, **controls}
    try:
        data = probe(cfg['source'])
        available, free = evaluate(data, cfg)
    except NotListed:
        result['errors'] = 0
        result['auth_failure_alerted'] = False
        result['unavailable_streak'] = result.get('unavailable_streak', 0) + 1
        result['last_status'] = 'not_listed'
        result['last_check'] = now
        result.pop('free_gpus', None)
        if result['unavailable_streak'] >= 2:
            result['alerted'] = False
        return result, 'NOT_LISTED: no matching resource in complete market listing'
    except AuthenticationError:
        result['errors'] = result.get('errors', 0) + 1
        result['unavailable_streak'] = 0
        result['last_status'] = 'auth_failed'
        result['last_check'] = now
        result.pop('free_gpus', None)
        return result, 'AUTH_FAILED: login session rejected; inventory unknown'
    except (ProbeError, ValueError) as exc:
        result['errors'] = result.get('errors', 0) + 1
        result['unavailable_streak'] = 0
        result['last_status'] = 'unknown'
        result['last_check'] = now
        return result, f'UNKNOWN: {exc}'
    result['errors'] = 0
    result['auth_failure_alerted'] = False
    result['last_check'] = now
    result['free_gpus'] = free
    if available:
        result['unavailable_streak'] = 0
        result['last_status'] = 'available'
        if not result.get('alerted'):
            target = cfg['target']
            if cfg['source'].get('kind') == 'gpushare_market':
                candidates = market_candidates(data, target)
                hosts = len(candidates)
                detail = '\n'.join(
                    f"- {row['region']} · 空闲 {row['free']} 张（主机 {row['machine_id']}）"
                    for row in candidates[:8])
                if hosts > 8:
                    detail += f'\n- 另有 {hosts - 8} 台候选机器'
            else:
                hosts = 1
                detail = '固定机器模式：' + str(target.get('machine_id', '未配置'))
            message = (f"GPUShare 云市场发现可租用的候选 GPU！\n\n"
                       f"GPU 型号：{target['gpu_model']}，单台需要 {target['gpu_count']} 张\n"
                       f"符合条件的机器数：{hosts}\n"
                       f"这些机器上的候选空闲卡合计：{free} 张\n"
                       f"候选机器详情：\n{detail}\n"
                       f"查询时间（UTC）：{now}\n\n"
                       f"请自行登录并尝试开机：{target.get('console_url', 'https://gpushare.com/')}\n\n"
                       "注意：这是云市场快照，不保证旧实例能直接启动。"
                       "如果原实例仍提示无卡，你可以在控制台手动评估迁移到可用机器；"
                       "迁移前请确认 /hy-tmp 数据已备份，官方文档提示迁移会清除该目录。\n"
                       "本服务不登录、不迁移、不启动、不购买任何 GPU。")
            control = read_control_config()
            if control is not None:
                message += control_section('pause', control)
            notify(f"【GPUShare】{target['gpu_model']} 出现候选空闲 GPU，请手动检查", message)
            result['alerted'] = True
            result['last_alert'] = now
            return result, 'AVAILABLE: mail sent'
        return result, 'AVAILABLE: already notified'
    result['last_status'] = 'unavailable'
    result['unavailable_streak'] = result.get('unavailable_streak', 0) + 1
    if result['unavailable_streak'] >= 2:
        result['alerted'] = False
    return result, f'UNAVAILABLE: {free} free'


def cycle_many(cfg, state, probe=fetch_json, notify=send_mail, now=None):
    """Keep independent inventory state and one shared mail-control channel."""
    result = dict(state)
    if state.get('paused'):
        return result, 'PAUSED: all inventory queries disabled'
    now = now or datetime.now(timezone.utc).isoformat(timespec='seconds')
    previous = state.get('target_states', {})
    current, messages = {}, []
    control_keys = {'paused', 'imap_uidvalidity', 'imap_last_uid', 'used_control_tokens'}
    for item in target_configs(cfg):
        key = target_signature(item)
        old = dict(previous.get(key, {}))
        if key not in previous and state.get('signature') == key:
            # Migrate the old single-machine deduplication state without resetting it.
            old = {k: v for k, v in state.items()
                   if k not in control_keys and k != 'target_states'}
        try:
            updated, message = cycle(item, old, probe=probe, notify=notify, now=now)
        except (OSError, smtplib.SMTPException, RuntimeError) as error:
            # A failed notification must not suppress this target or skip the next one.
            updated = {**old, 'signature': key, 'last_status': 'notification_failed',
                       'last_check': now, 'errors': old.get('errors', 0) + 1}
            message = f'NOTIFICATION_FAILED: {type(error).__name__}'
        current[key] = updated
        target = item['target']
        messages.append(f"{target['gpu_model']} {target.get('machine_id', 'any')}: {message}")
    result['target_states'] = current
    result.pop('signature', None)
    result.pop('alerted', None)
    result.pop('unavailable_streak', None)
    statuses = [s['last_status'] for s in current.values()]
    result['last_check'] = now
    # Authentication alerts remain shared and are sent even if the other query succeeds.
    for status in ('auth_failed', 'unknown', 'notification_failed', 'available', 'unavailable', 'not_listed'):
        if status in statuses:
            result['last_status'] = status
            break
    failures = [s for s in current.values()
                if s['last_status'] in ('auth_failed', 'unknown', 'notification_failed')]
    result['errors'] = max((s.get('errors', 0) for s in failures), default=0)
    if failures:
        result.pop('free_gpus', None)
    else:
        result['auth_failure_alerted'] = False
        result['free_gpus'] = sum(s.get('free_gpus', 0) for s in current.values())
    return result, ' | '.join(messages)


def health_mail_if_needed(state, notify=send_mail, now=None):
    now = time.time() if now is None else now
    if state.get('last_status') == 'auth_failed':
        if (state.get('auth_failure_alerted') and
                now - state.get('last_auth_alert_epoch', 0) < 6 * 3600):
            return state
        notify('【GPUShare】登录态失效或权限不足，请检查 Token',
               'GPUShare 只读市场接口拒绝了认证，当前无法可靠查询 GPU 库存。\n'
               '可能原因：Token 已过期、已被撤销，或账号没有访问权限。\n\n'
               '请自行在浏览器登录 GPUShare，获取新的 Authorization 请求头，'
               '仅在服务器本地更新项目目录的 .env 中的\n'
               'GPUSHARE_READ_AUTHORIZATION="Bearer ..."\n'
               '然后运行：sudo systemctl restart gpushare-monitor.service\n\n'
               '不要通过邮件或聊天发送 Token。程序不会自动登录或刷新 Token。\n'
               '持续认证失败时每 6 小时最多提醒一次；成功查询后重新计数。')
        state['auth_failure_alerted'] = True
        state['last_auth_alert_epoch'] = now
        LOG.info('Authentication failure alert submitted to SMTP')
        return state
    if state.get('errors', 0) < 5:
        return state
    if now - state.get('last_error_alert_epoch', 0) < 6 * 3600:
        return state
    notify('【GPUShare】监测连续失败，需要检查',
           'GPUShare 库存检测连续失败至少 5 次。请检查网络、接口格式、登录态及服务日志。\n'
           '监控失败时不会发送“有卡”通知。')
    state['last_error_alert_epoch'] = now
    return state


def poll_delay(cfg, state, elapsed=0):
    """Schedule rounds 30–40 seconds apart, including normal processing time."""
    interval = cfg.get('poll_seconds', 30) + random.uniform(0, 10)
    if state.get('last_status') in ('unknown', 'auth_failed'):
        interval = max(interval, min(900, 60 * (2 ** min(state.get('errors', 1)-1, 4))))
    return max(1.0, interval - elapsed)


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
    parser.add_argument('command', choices=['check', 'once', 'monitor', 'test-mail', 'test-control', 'inspect-json', 'inspect-live'])
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--state', default='state/state.json')
    parser.add_argument('--sample-file', help='离线 JSON 样本，仅用于 inspect-json；不联网、不发邮件')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'test-mail':
        send_mail('【GPUShare】监控邮件测试', '测试成功。此邮件不代表发现空闲显卡。')
        LOG.info('邮件测试发送成功')
        return 0
    if args.command == 'test-control':
        control = read_control_config()
        if control is None:
            raise ValueError('邮件控制未启用，请先在 .env 填写 IMAP 和 CONTROL_* 参数')
        text = '这是一封邮件控制功能测试，不代表发现空闲 GPU。'
        send_mail('【GPUShare】邮件暂停控制测试（非库存提醒）',
                  text + control_section('pause', control))
        LOG.info('暂停测试邮件发送成功')
        return 0
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8'))
    configs = target_configs(cfg)
    if args.command == 'inspect-json':
        if not args.sample_file:
            raise ValueError('inspect-json 需要 --sample-file 文件路径')
        sample_path = Path(args.sample_file)
        if sample_path.stat().st_size > MAX_RESPONSE:
            raise ValueError('样本文件超过 1 MiB，不予处理')
        data = json.loads(sample_path.read_text(encoding='utf-8'))
        print(describe_json_shape(data))
        for item in configs:
            print(f"--- {item['target']['gpu_model']} 样例检查（不触发邮件）---")
            if item['target'].get('match_mode') == 'gpu_model':
                try:
                    available, free = evaluate_market(data, item['target'])
                    print(f'available={available}, eligible_free_gpus={free}')
                except ProbeError as exc:
                    print(f'无法确认市场库存：{exc}')
            else:
                print(inspect_json(data, str(item['target']['machine_id'])))
        return 0
    if args.command == 'inspect-live':
        # Explicit, single, read-only request only. No need to set confirmed_read_only.
        for item in configs:
            validate_market_diagnostic(item['source'])
            data = fetch_json(item['source'])
            print(f"--- {item['target']['gpu_model']} 只读 GET 检查 ---")
            print(describe_json_shape(data))
            if item['target'].get('match_mode') == 'gpu_model':
                try:
                    available, free = evaluate_market(data, item['target'])
                    print(f'available={available}, eligible_free_gpus={free}')
                except ProbeError as exc:
                    print(f'无法确认市场库存：{exc}')
            else:
                print(inspect_json(data, str(item['target']['machine_id'])))
        return 0
    validate(cfg)
    if args.command == 'check':
        exit_code = 0
        for item in configs:
            target = item['target']
            label = f"model={target['gpu_model']} machine={target.get('machine_id', 'any')}"
            try:
                avail, free = evaluate(fetch_json(item['source']), item)
                print(f"{'AVAILABLE' if avail else 'UNAVAILABLE'} eligible_free={free} {label}")
            except NotListed:
                print(f'NOT_LISTED: no matching resource in complete listing; {label}')
            except ProbeError as exc:
                print(f'UNKNOWN: {exc}; {label}')
                exit_code = 2
        return exit_code
    control = read_control_config() if args.command == 'monitor' else None
    with process_lock(args.state):
        while True:
            round_started = time.monotonic()
            state = load_state(args.state)
            if control is not None:
                try:
                    state, actions = scan_mail_controls(state, control)
                    # Persist accepted commands before acknowledgement or market polling.
                    save_state(args.state, state)
                    for action in actions:
                        message = (f'GPUShare 监测已{"暂停" if action == "pause" else "恢复"}。\n'
                                   '此操作适用于全部监控目标，不会控制云端实例。')
                        message += control_section('resume' if action == 'pause' else 'pause', control)
                        try:
                            send_mail(f'【GPUShare】监测已{"暂停" if action == "pause" else "恢复"}', message)
                        except (OSError, smtplib.SMTPException, RuntimeError) as exc:
                            LOG.error('邮件控制确认通知失败：%s', type(exc).__name__)
                except Exception as exc:
                    # Fail closed: no automatic mail control changes when IMAP fails.
                    # Do not print credential-bearing exception messages.
                    LOG.error('邮件控制检查失败（监测状态保持不变）：%s', type(exc).__name__)
            if state.get('paused'):
                LOG.info('PAUSED: email control; only checking for resume messages')
                if args.command == 'once':
                    return 0
                time.sleep(poll_delay(cfg, {}, time.monotonic() - round_started))
                continue
            try:
                if len(configs) > 1 or 'target_states' in state:
                    state, msg = cycle_many(cfg, state)
                else:
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
                return 1 if state.get('last_status') in ('unknown', 'auth_failed', 'notification_failed') else 0
            time.sleep(poll_delay(cfg, state, time.monotonic() - round_started))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, ProbeError, RuntimeError, OSError, json.JSONDecodeError) as error:
        LOG.error('%s', error)
        sys.exit(2)
