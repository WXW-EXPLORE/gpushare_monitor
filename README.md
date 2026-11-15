# GPUShare Monitor

GPUShare GPU 库存监控与邮件提醒工具。选择显卡型号或指定机器，发现满足条件的空闲 GPU 后发送邮件，由使用者在网页中手动确认和开机。

使用 Python 标准库，无需安装第三方依赖；支持前台运行和 Linux systemd 部署，不需要 Docker，也不监听公网端口。程序只查询市场库存，不会自动登录、刷新登录凭据、创建实例、启动实例或执行付费操作。

## 功能

- **两种匹配方式**：按显卡型号查找任意机器，或同时匹配机器 ID 和显卡型号。
- **多个监控目标**：可以监控不同型号、不同机器，每个目标分别去重和判断重新上架。
- **完整分页检查**：避免只查第一页；分页不完整、数据异常或认证失败时不发送有卡提醒。
- **邮件通知**：包含型号、机器 ID、地区、符合条件的空闲数量及手动访问入口。
- **登录态失效提醒**：首次检测到认证失败时立即尝试发送邮件，避免把查询失败误认为没有库存。
- **邮件暂停/恢复**：点击邮件中的控制链接、手动发送预填邮件后，统一暂停或恢复全部监控目标。
- **持久化状态**：重启后保留去重记录、暂停状态和邮件控制游标。

## 运行要求

- Python 3.10 或更新版本；运行环境为 Linux。
- 能访问 GPUShare 市场接口，以及配置的 SMTP 服务。
- 若启用邮件控制，需要邮箱支持 IMAP SSL。
- 若接口要求认证，需要使用者自行获取有效 Bearer Token。
- 使用后台部署和快捷 Token 更新功能时，需要 systemd 及管理该服务的权限。

这是基于市场列表接口实现的工具。接口字段、访问权限和登录会话策略可能变化，部署时应先验证响应与网页显示一致。邮件中的库存是查询时的快照，不保证收到邮件时仍可租用，也不保证已有实例能在对应机器启动。

## 快速开始

```bash
git clone https://github.com/WXW-EXPLORE/gpushare_monitor.git
cd gpushare_monitor
cp .env.example .env
```

接下来创建 `config.json` 并填写 `.env`。配置完成后设置私有文件权限：

```bash
chmod 600 config.json .env
mkdir -p state
chmod 700 state
```

### 1. 选择监控目标

以下以 `4090-48G` 为例，型号可换成接口实际返回的其他名称。请使用准确的型号字符串；主目标的 `target.gpu_model` 与查询 URL 中的 `gpuName` 必须一致。

**按型号监控任意机器**：将以下内容保存为 `config.json`。

```json
{
  "target": {
    "match_mode": "gpu_model",
    "gpu_model": "4090-48G",
    "gpu_count": 1,
    "console_url": "https://gpushare.com/"
  },
  "poll_seconds": 30,
  "source": {
    "kind": "gpushare_market",
    "confirmed_read_only": true,
    "url": "https://api.gpushare.com/app/api/market/machine/list?models=-1&gpuName=4090-48G&priceOrder=asc&pn=1&ps=10&minGpuNum=1&maxGpuNum=24&vmType=docker&tag=",
    "method": "GET",
    "headers": {
      "Authorization": "env:GPUSHARE_READ_AUTHORIZATION"
    },
    "max_pages": 30
  }
}
```

**只监控指定机器**：将上述 `target` 对象替换为以下内容，并填写自己的机器 ID。

```json
{
  "match_mode": "machine",
  "machine_id": "YOUR_MACHINE_ID",
  "gpu_model": "4090-48G",
  "gpu_count": 1,
  "console_url": "https://gpushare.com/"
}
```

**增加其他目标**：在 `config.json` 根对象中增加 `additional_targets` 字段。例如，保留主目标并额外监控一台 A6000 机器：

```json
{
  "additional_targets": [
    {
      "match_mode": "machine",
      "machine_id": "YOUR_SECOND_MACHINE_ID",
      "gpu_model": "A6000-48G",
      "gpu_count": 1,
      "console_url": "https://gpushare.com/"
    }
  ]
}
```

这段是需要合并到原配置中的字段，不是独立配置文件。新增目标也可以使用 `gpu_model` 模式；每个新增目标的型号查询参数由程序生成，无需为它单独填写 URL。同一机器 ID 不能重复作为精确目标添加；按型号监控时，同一型号不能重复添加。

| 配置项 | 含义 |
|---|---|
| `match_mode` | `machine` 精确匹配机器；`gpu_model` 匹配该型号的任意机器 |
| `machine_id` | `machine` 模式必填，来自平台返回的机器 ID |
| `gpu_model` | 接口中的 GPU 型号名称，注意大小写和后缀 |
| `gpu_count` | 同一台机器至少需要的空闲 GPU 数，不能跨机器相加满足条件 |
| `console_url` | 通知邮件中的手动访问入口 |
| `poll_seconds` | 基础轮询周期，至少 30 秒；每轮另加 0–10 秒随机间隔 |
| `max_pages` | 每个型号的最大查询页数，默认 30；超过上限视为结果不完整 |

查询 URL 中的 `minGpuNum`、`maxGpuNum` 也会影响返回范围，应按需求调整，避免把目标资源过滤掉。型号包含空格等特殊字符时，URL 中的 `gpuName` 需要进行 URL 编码。

当前按 `gpuNum - gpuUsed` 计算市场可见空闲数量，同时检查机器在线、状态正常且按量计费 `payg` SKU 可用。精确机器模式下，其他机器有空闲卡不会触发通知。

### 2. 配置 GPUShare 凭据

如果只读接口要求认证，在自己的浏览器登录 GPUShare，打开开发者工具的 **Network / 网络**，找到市场列表 `machine/list` 请求，查看请求头中的 `Authorization`。

仅在本机 `.env` 中填写：

```dotenv
GPUSHARE_READ_AUTHORIZATION="Bearer YOUR_PRIVATE_TOKEN"
```

`config.json` 中只保存 `env:GPUSHARE_READ_AUTHORIZATION` 这样的环境变量引用。若已确认接口支持匿名查询，可将 `source.headers` 设为 `{}`，无需配置 Token。

网页会话可能过期或被撤销；程序不会自动登录或续期。不要把真实 Token 放入命令行参数、提交到仓库或贴到聊天、Issues 中。

### 3. 配置 SMTP

在 `.env` 中填写自己邮箱服务商提供的 SMTP 参数。以下地址为占位示例：

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=465
SMTP_FROM=monitor@example.com
SMTP_TO=you@example.com
SMTP_USER=monitor@example.com
SMTP_PASSWORD="YOUR_SMTP_APP_PASSWORD"
```

端口 465 使用 SMTP SSL，其他端口使用 STARTTLS。`SMTP_USER` 和 `SMTP_PASSWORD` 需同时设置或同时留空；使用邮箱账号认证时，按服务商要求填写专用授权码。

程序不会自动加载 `.env`，手动执行命令前需在项目目录导入配置。修改文件后也需要重新导入：

```bash
set -a
. ./.env
set +a
```

### 4. 验证后再启动

先运行离线测试、只读检查和测试邮件：

```bash
python3 -m unittest discover -s tests -v
python3 gpushare_monitor.py check --config config.json
python3 gpushare_monitor.py test-mail
```

`check` 会分别检查所有目标，不发库存邮件、不修改云端数据。请核对网页中的型号、机器 ID、空闲数量，并确认测试邮件确实收到。接口返回认证失败或无法解释的结果时，先解决问题再启用正式通知。

验证通过后可前台运行：

```bash
python3 gpushare_monitor.py monitor --config config.json
```

按 `Ctrl+C` 结束前台运行，再按下文部署后台服务。

## 运行命令与通知规则

| 命令 | 用途 |
|---|---|
| `check --config config.json` | 每个目标做一次完整只读检查，不发送库存邮件 |
| `once --config config.json` | 检查一轮，按库存和去重状态发送通知 |
| `monitor --config config.json` | 持续监测，可配合 IMAP 控制 |
| `test-mail` | 发送一封与库存无关的测试邮件 |
| `test-control` | 发送带暂停链接的控制测试邮件，需要启用邮件控制 |
| `inspect-live --config config.json` | 只读诊断响应结构，不打印原始字段值 |

以上命令均使用 `python3 gpushare_monitor.py` 调用。默认状态文件为 `state/state.json`，可用 `--state` 指定其他路径。

正常每轮约 30–40 秒，处理耗时计入周期；多目标时每轮分别查询各型号并遍历分页。接口变慢或查询失败时周期会延长，异常退避最长 15 分钟。

- 每个目标第一次满足条件时发邮件，持续可用时不重复发送。
- 连续两次完整扫描均无符合条件的资源后，该目标重新具备提醒资格；查询异常不会被当作无库存来重置状态。
- 多目标分别记录状态，一台的持续可用不会阻止另一台重新上架提醒；单台查询或库存邮件发送失败，不跳过同轮其他目标。
- 首次发现 HTTP 或业务状态 401/403 时，立即尝试发送认证失败提醒；持续失败每 6 小时最多提醒一次，成功查询后允许下一次认证失败重新提醒。
- 其他查询错误连续出现 5 次后，每 6 小时最多发送一次异常提醒。SMTP 本身故障时通知无法保证送达，需结合日志检查。

## 邮件暂停与恢复（可选）

该功能通过 SMTP/IMAP 收发控制邮件，不需要域名、网页控制服务或入站端口。

在 `.env` 中增加以下配置：

```dotenv
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_USER=monitor@example.com
IMAP_PASSWORD="YOUR_IMAP_APP_PASSWORD"
CONTROL_AUTHORIZED_FROM=you@example.com
CONTROL_SECRET=REPLACE_WITH_A_RANDOM_SECRET
```

`IMAP_USER` 是程序监听的收件箱。`CONTROL_AUTHORIZED_FROM` 是允许发送控制指令的邮箱地址，应与邮件客户端实际发件地址一致。IMAP 使用 SSL；授权码和服务器地址以邮箱服务商设置为准。

在服务器本地生成控制密钥，并将输出保存到 `.env` 的 `CONTROL_SECRET` 中：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

密钥至少 32 字节，保存后保持不变。除端口有默认值外，上述控制参数必须全部提供；完全不填写则禁用邮件控制。修改后重启监控进程，使配置生效。

操作流程：

1. 点击库存通知或控制测试邮件中的链接，邮件客户端会打开一封预填邮件。
2. 手动点击“发送”；程序校验发件地址、有效期及 HMAC 签名后执行操作。
3. 暂停成功后收到确认邮件，其中提供恢复链接；点击并发送预填邮件即可恢复。

控制签名位于预填邮件主题中，保持主题不变。它是一次性的控制凭证，不包含 GPUShare Token，48 小时内有效；不要转发含控制链接的邮件。

暂停统一作用于全部目标：停止库存查询，保留 IMAP 监听。恢复后全部目标重新具备库存提醒资格。IMAP 使用只读方式查看收件箱，不设置已读或删除邮件；首次初始化跳过已有邮件，因此要先启动服务，再发送控制指令。

测试邮件控制时，在服务已启动、IMAP 已初始化后运行：

```bash
python3 gpushare_monitor.py test-control
```

手动发送暂停指令，确认收到暂停回执且日志显示 `PAUSED`；再发送恢复指令，确认恢复回执和后续库存查询。该测试邮件不代表发现空闲 GPU。

## 使用 systemd 后台部署

在普通 Linux 用户下完成配置和前述验证，结束前台监控后，运行以下命令。服务中的路径和用户会根据当前项目目录自动填写：

```bash
project_dir="$(pwd -P)"
run_user="$(id -un)"
run_group="$(id -gn)"
python_path="$(command -v python3)"
unit_file="$(mktemp)"
mkdir -p state
chmod 700 state
chmod 600 .env config.json

cat > "$unit_file" <<EOF
[Unit]
Description=GPUShare read-only GPU market monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${run_user}
Group=${run_group}
WorkingDirectory=${project_dir}
EnvironmentFile="${project_dir}/.env"
ExecStart="${python_path}" "${project_dir}/gpushare_monitor.py" monitor --config "${project_dir}/config.json" --state "${project_dir}/state/state.json"
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths="${project_dir}/state"
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 644 "$unit_file" /etc/systemd/system/gpushare-monitor.service
rm -- "$unit_file"
sudo systemctl daemon-reload
sudo systemctl enable --now gpushare-monitor.service
```

该服务启用开机启动、异常退出重启，并将日志写入 journal。程序只连接配置中的外部服务，无需更改已有网站、反向代理或邮件服务。也可参考 `systemd/gpushare-monitor.service.example`，替换其中的用户、组和目录后安装。

常用操作：

```bash
# 查看服务和日志
sudo systemctl status gpushare-monitor.service
sudo journalctl -u gpushare-monitor.service -n 50 --no-pager
sudo journalctl -u gpushare-monitor.service -f

# 完全停止，包括 IMAP 监听
sudo systemctl stop gpushare-monitor.service

# 启动进程；已保存的邮件暂停状态仍然有效
sudo systemctl start gpushare-monitor.service

# 修改配置或凭据后重新加载
sudo systemctl restart gpushare-monitor.service
```

`active (running)` 只表示进程在运行；还需查看是否为 `PAUSED`、是否有新的查询记录以及接口状态。

## 快捷更新 Token

已安装上述 systemd 服务后，在项目目录运行：

```bash
python3 update_token.py
```

在隐藏输入提示中粘贴新 Token 并回车。可粘贴纯 Token、`Bearer ...` 或完整的 `Authorization: Bearer ...`。脚本会先验证所有目标的只读查询，通过后自动替换 `.env` 中的旧值、保持权限 `600`，并重启 `gpushare-monitor.service`。验证失败保留旧配置；保存成功但重启失败时，会提示手动重启。

快捷命令使用脚本所在项目目录的 `.env` 和 `config.json`，并要求配置中的 Authorization 引用 `env:GPUSHARE_READ_AUTHORIZATION`。重启会保留邮件暂停状态，不会自动解除暂停。前台运行模式可手动编辑 `.env`，重新导入环境变量后再启动进程。

## 状态与排查

| 状态 | 含义与处理 |
|---|---|
| `AVAILABLE` | 对应目标有满足条件的候选库存；到网页手动确认 |
| `UNAVAILABLE` | 目标已返回，但当前没有满足租用条件的空闲卡 |
| `NOT_LISTED` | 完整筛选结果没有该型号或指定机器；可能因库存筛选而未返回，不等同于准确剩余量为零 |
| `AUTH_FAILED` | 认证失效或权限不足；手动获取有效 Token 并更新 |
| `UNKNOWN` | 网络、业务响应、字段或分页异常；无法确认库存 |
| `NOTIFICATION_FAILED` | 库存邮件发送失败；检查 SMTP 设置及日志 |
| `PAUSED` | 库存查询已暂停，仍监听恢复邮件 |

`check` 命令中的认证错误以 `UNKNOWN` 和具体原因输出；守护进程会单独记录 `AUTH_FAILED` 并触发告警。

收不到控制回执时，先确认邮件已发往 `IMAP_USER`、发件地址与 `CONTROL_AUTHORIZED_FROM` 一致、主题未改动且签名未过期。库存异常退避期间，控制邮件检查也可能延后。

如果暂停超过 48 小时、恢复链接已经过期，可启动服务、导入 `.env`，在本机重新发送一封恢复链接邮件，再通过邮件客户端手动确认发送：

```bash
python3 - <<'PY'
from email_control import read_control_config, control_section
from gpushare_monitor import send_mail
control = read_control_config()
if control is None:
    raise SystemExit('请先启用邮件控制')
send_mail('GPUShare：新的恢复监控链接', control_section('resume', control))
PY
```

接口和 SMTP/IMAP 联调完成前，不应仅凭进程在运行就认定部署成功。日常分享仓库时保留示例配置即可，勿提交 `.env`、真实 `config.json`、状态文件或原始接口响应。
