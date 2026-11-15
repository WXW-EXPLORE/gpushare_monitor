#!/usr/bin/env python3
"""Interactively validate and replace the local GPUShare read-only credential."""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import warnings

from gpushare_monitor import AuthenticationError, ProbeError, fetch_json, target_configs, validate

KEY = 'GPUSHARE_READ_AUTHORIZATION'
ROOT = Path(__file__).resolve().parent


def normalize_token(value):
    value = value.strip()
    value = re.sub(r'^Authorization:\s*', '', value, flags=re.IGNORECASE)
    value = re.sub(r'^Bearer\s+', '', value, flags=re.IGNORECASE)
    # RFC 6750 token characters also avoid shell expansion in the .env file.
    if not re.fullmatch(r'[A-Za-z0-9._~+/\-]+=*', value):
        raise ValueError('请粘贴 Token，或完整的 Authorization: Bearer 请求头值；不要包含引号或换行')
    return 'Bearer ' + value


def replace_authorization(text, authorization):
    authorization = normalize_token(authorization)
    entry = f'{KEY}="{authorization}"\n'
    pattern = re.compile(r'^\s*(?:export\s+)?' + KEY + r'\s*=')
    lines, replaced = [], False
    for line in text.splitlines(keepends=True):
        if pattern.match(line):
            if not replaced:
                lines.append(entry)
                replaced = True
        else:
            lines.append(line)
    if not replaced:
        if lines and not lines[-1].endswith(('\n', '\r')):
            lines.append('\n')
        lines.append(entry)
    return ''.join(lines)


def write_env(path, authorization):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('请使用项目目录内的普通 .env 文件')
    text = replace_authorization(path.read_text(encoding='utf-8'), authorization)
    fd, temporary = tempfile.mkstemp(prefix='.env-update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update(env_path, config_path, authorization):
    """Validate first; rejected credentials leave the local file untouched."""
    authorization = normalize_token(authorization)
    cfg = json.loads(Path(config_path).read_text(encoding='utf-8'))
    validate(cfg)
    if cfg['source'].get('headers', {}).get('Authorization') != 'env:' + KEY:
        raise ValueError('config.json 的 Authorization 未引用预期的本地环境变量')
    previous = os.environ.get(KEY)
    try:
        os.environ[KEY] = authorization
        for item in target_configs(cfg):
            fetch_json(item['source'])
    finally:
        if previous is None:
            os.environ.pop(KEY, None)
        else:
            os.environ[KEY] = previous
    write_env(env_path, authorization)


def main():
    if not sys.stdin.isatty():
        print('请直接在服务器终端运行本命令，然后在隐藏提示中粘贴 Token。', file=sys.stderr)
        return 2
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            value = getpass.getpass('粘贴新 Token（可带 Bearer，输入隐藏），然后回车：')
        print('正在验证官方只读查询…')
        update(ROOT / '.env', ROOT / 'config.json', value)
    except AuthenticationError:
        print('认证失败，旧配置未修改。请重新获取有效 Token。', file=sys.stderr)
        return 1
    except (ProbeError, ValueError, OSError, getpass.GetPassWarning):
        print('验证或保存失败，请检查 Token、网络及配置权限；凭据不会打印。', file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\n已取消。', file=sys.stderr)
        return 1
    command = ['systemctl', 'restart', 'gpushare-monitor.service']
    if os.geteuid() != 0:
        command.insert(0, 'sudo')
    try:
        subprocess.run(command, check=True)
    except (subprocess.CalledProcessError, OSError):
        print('Token 验证并保存成功（权限 600）；请手动运行 sudo systemctl restart gpushare-monitor.service', file=sys.stderr)
        return 1
    print('Token 验证成功，已替换旧值（权限 600）并重启 GPUShare 监控。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
