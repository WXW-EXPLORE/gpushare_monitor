# GPUShare Monitor — 仅监测与邮件提醒

针对你所需的 **4080s-32G × 1** 监测 GPUShare 云市场中的**任意机器**。只要有至少一台机器满足空闲卡数及按量计费条件，就发送邮件，由你自行登录网站查看并开机。不再绑定某个 `machineId`，也不限制地区。**绝不自动登录、开机、购买、充值、停止或修改实例。** 使用 Python 标准库运行，不需要 Docker。

## 当前进度与限制

- ✅ 已根据 2026-10-23 的实际浏览器请求确认：`GET https://api.gpushare.com/app/api/market/machine/list`；并根据真实响应确认字段 `data.list`、`data.total`、`machineId`、`gpuName`、`gpuNum`、`gpuUsed`、`regionName`、`isOnline`、`status` 和 `skuList`。
- ✅ 采用 `gpuNum - gpuUsed` 计算该主机**市场可见的空闲数量**，按显卡型号筛选所有机器，并检查每台机器的空闲卡数、在线状态及按量计费 SKU；自动翻页，避免只看第 1 页漏报。
- ✅ 可用时发一封邮件，持续可用时不重复发送。该型号两次在**完整市场列表**中无可用候选资源之后，可以再次触发提醒。错误或请求失败一律不发送“有卡”邮件。
- ⚠️ **还没有通过你的账号实测该接口是否需要 Bearer 登录态，以及市场可见空闲 GPU 是否允许你的已有实例成功开机。** 因而完成代码和模拟测试，不等于已经开通真实监控。云市场可租不保证“已有实例”一定能成功启动。
- ⚠️ 先前截图中曾显示完整 `Authorization: Bearer`，请撤销/更新那份登录会话。**不要把 Token、Cookie、API 响应中的个人资料提交到仓库、聊天或 Issues。**

## 监控范围：按显卡型号，不限主机/地区

默认模板 `config.my-instance.example.json` 已更新为：

| 属性 | 值 |
|---|---|
| `target.match_mode` | `gpu_model`（不匹配机器 ID） |
| GPU | `4080s-32G` × 1 |
| 地区 | **不限**：所有响应中符合型号的地区 |
| 触发条件 | **任意一台**机器在线、按量 `payg` 可用且 `gpuNum - gpuUsed >= 1` |

**注意：** 你原先的按量实例有可能与物理宿主机绑定。其他机器有同型号的可租显卡，只能说明市场上有该 GPU 的候选库存，**不能保证旧实例能在另一台机器启动**。如果你实际可以迁移/重新创建同配置实例，型号监控更符合目标；但监控程序本身不会执行任何实例操作。GPUShare [官方迁移文档](https://gpushare.com/docs/instance/migration/) 介绍了手动迁移至同型号 GPU 主机的方法，且提醒迁移会清除 `/hy-tmp`；如需迁移，请先备份重要数据。

假如两台目标型号机器各有 1 张空闲卡，邮件报告为“2 台候选主机、合计 2 张”；如果需要 2 张则必须**同一台机器**至少 2 张空闲，不能跨机器拼凑。机器名称/机器 ID 只在邮件中作为可参考信息，不参与库存匹配条件。

## 一、安装和配置

```bash
git clone https://github.com/WXW-EXPLORE/gpushare_monitor.git
cd gpushare_monitor
cp config.my-instance.example.json config.json
cp .env.example .env
chmod 600 config.json .env
python3 -m unittest discover -s tests -v
```

模板中的 `source.kind=gpushare_market` 和 `confirmed_read_only=true` 只允许已观察到的那个只读 `GET /app/api/market/machine/list`。仅允许 HTTPS 官方域名、禁止重定向；任何登录失效、响应业务 `status != 0`、分页不完整或关键字段格式异常，都不会触发有卡提醒。

`source.max_pages` 默认 30，`ps=10` 每页最多 10 条。程序先检查 `data.total` 再依次请求每一页；结果不完整或中途变化均视为 `UNKNOWN`。如果总页数经常很多，应延长轮询间隔、与平台确认合适的请求频率。

## 二、由你自己完成登录（仅当必要时）

先在**不保存任何账号信息**的状态下测试该 API 是否支持匿名只读查询：

```bash
python3 gpushare_monitor.py check --config config.json
```

如果返回 `AVAILABLE` / `UNAVAILABLE` / `NOT_LISTED`，说明请求获取了格式正确的结果；仍需人工确认与你网页看到的库存一致。`NOT_LISTED` 不代表真实剩余量是 0。

如果输出 `HTTP 401/403` 或业务状态失败，说明可能需要登录态。此时你自己在浏览器完成登录并从 `Network → Headers` 获取**新的** `Authorization`；不需要向开发者或 AI 发送该令牌，也不要复制旧截图中的令牌。在服务器本地 `.env` 增加：

```bash
GPUSHARE_READ_AUTHORIZATION="Bearer YOUR_NEW_PRIVATE_TOKEN"
```

随后**只在本机**编辑 `config.json` 的 `source.headers`：

```json
"headers": { "Authorization": "env:GPUSHARE_READ_AUTHORIZATION" }
```

此处是手动保存已经登录会话的必要凭据，并非程序替你登录；**只有在平台允许使用会话进行只读轮询时才这样做**。如果不愿存储令牌，且匿名接口不可用，就不能稳定实现全天候监控。令牌失效后自行重新登录并替换；程序不会自动获取或刷新凭据。

`test-mail`、`check` 等命令若在 shell 中手工运行，需要先从私有 `.env` 导入环境变量：

```bash
set -a; . ./.env; set +a
```

## 三、配置邮件，试运行

编辑本机 `.env`，填写自己的 SMTP 邮箱信息（可以使用邮箱提供商的专用 SMTP 授权码，**不要填网页登录密码**）：

```text
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_FROM=your_account@qq.com
SMTP_TO=destination@example.com
SMTP_USER=your_account@qq.com
SMTP_PASSWORD=YOUR_SMTP_APP_PASSWORD
```

```bash
python3 gpushare_monitor.py test-mail
python3 gpushare_monitor.py check --config config.json
python3 gpushare_monitor.py once --config config.json
python3 gpushare_monitor.py monitor --config config.json
```

- `test-mail` 只发送测试邮件，与 GPU 库存无关；
- `check` 只检查一次，不发邮件、不更改云端状态；
- `once` 只检查一次，如果观察到至少一台同型号机器满足条件才发送邮件；
- `monitor` 循环监测（默认 60 秒加 0–10 秒随机间隔，失败时退避，最长 15 分钟）。

邮件主题为「4080s 型号出现候选空闲 GPU，请手动检查」。邮件中的空闲数量是云市场快照，随时可能变化。

## 四、在已有轻量服务器独立部署

将 `systemd/gpushare-monitor.service.example` 复制为系统服务配置，把 `REPLACE_USER` 和 `REPLACE_PROJECT_PATH` 改为实际用户名和项目目录。**不要重启 Nginx、Docker 或任何已有网站、邮件服务。**

```bash
sudo cp /tmp/gpushare-monitor.service /etc/systemd/system/gpushare-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now gpushare-monitor.service
systemctl status gpushare-monitor.service
journalctl -u gpushare-monitor.service -f
# 停止：sudo systemctl stop gpushare-monitor.service
```

systemd 从私有 `.env` 注入凭据，监控进程不保存原始接口响应及 Token。`state/state.json` 持久化邮件去重状态，文件锁避免同一实例多个进程重复提醒。连续 5 次探测失败可以每 6 小时发一次异常提醒。

建议先只运行 `check`，确认解析结果与网页同型号库存一致，再运行 `monitor`。若日志反复显示 `UNKNOWN`，不要误认为平台真的没有显卡。

## 五、故障与风险说明

- `NOT_LISTED`：完整市场列表中没有返回所需型号的机器。不表示你原来那台实例必然不可开机。
- `UNKNOWN`：接口失败、登录态过期、分页被截断、市场变化或字段异常。保守处理，不发送有卡邮件。
- `AVAILABLE`：完整市场列表中至少有一台同型号机器空闲数量满足要求、在线且 payg SKU 可用，**不保证已关机实例启动成功**。
- 由于 GPU 极易被他人抢走，邮件发出时库存可能已经变化；这是通知方案的自然限制。
- `.env`、`config.json`、Token 和 Cookie 均不得提交到 GitHub；运行前遵守 GPUShare 的接口与使用规则。
- 关机实例的到期、系统盘和 `/hy-tmp` 数据保留条件以你控制台显示的现行规则为准，重要研究资料请单独备份。

## 六、测试

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile gpushare_monitor.py
```

本地测试使用合成的 JSON 响应，包括你提供的 TITAN X 示例中 `gpuNum=7, gpuUsed=6` 的字段逻辑，不需要也不会使用你的任何个人登录令牌。

## 七、邮件控制：点击邮件里的链接暂停/恢复（无需新域名或公网端口）

可选功能，不改变 GPUShare 实例、不会代替你登录或开机。**为不修改现有服务器的网站、反向代理或防火墙，本项目采用 `mailto:` 控制邮件，而不是公开网页上的一键 GET 操作。**

**实际使用步骤：**

1. 云市场出现候选 4080s-32G 时，通知邮件末尾显示「点此发送监控控制邮件」链接（邮件应用支持 HTML 时可直接点击）。
2. 点击后，手机/电脑默认邮件 App 打开一封**预填好的邮件**，收件人是监控邮箱，主题是一条有签名、48 小时内有效的 `GPUCTRL PAUSE ...` 指令。**你需要自己点击“发送”才会暂停**，邮件预览/链接扫描不会改变状态。
3. Linux 后台大约每 60 秒通过 IMAP 只读检查新邮件（**不设已读、不删除邮件**）。仅当发件人是你在 `CONTROL_AUTHORIZED_FROM` 填写的地址且 HMAC 签名有效才接受暂停。暂停后不会再请求 GPUShare，只轮询邮箱中的「恢复」指令。
4. 程序发出「监测已暂停」确认邮件，其中含新的「恢复监测」链接。点击后**发送**预填邮件即可恢复库存监测。

**额外配置**：在服务器本机 `.env` 填入以下环境变量（不要提交 GitHub）：

```dotenv
IMAP_HOST=imap.qq.com
IMAP_PORT=993
IMAP_USER=your_account@qq.com
IMAP_PASSWORD=YOUR_IMAP_APP_PASSWORD
CONTROL_AUTHORIZED_FROM=your_notification_recipient@example.com
CONTROL_SECRET=GENERATE_A_NEW_PRIVATE_SECRET
```

- `IMAP_USER`：邮件监控程序自己的**收件邮箱**。通常与 `SMTP_FROM` 相同。需在服务商中启用 IMAP 并使用专用邮箱授权码；IMAP 授权码不是 GPUShare 登录密码。
- `CONTROL_AUTHORIZED_FROM`：你在手机上回复控制邮件时使用的**发件邮箱**，通常与你收到提醒的 `SMTP_TO` 相同；如果邮箱客户端实际使用别名，应填准确发件地址。
- `CONTROL_SECRET`：在服务器上执行 `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成，保持私密（最少 32 字节）。只在本地 `.env` 保存。**保留此值，以免旧控制邮件失效。**
- `.env` 中 **五个必填参数必须全部提供** 才启用邮件控制；一个都不填写则保持原有普通邮件提醒，无链接和 IMAP 轮询。
- 首次启动时 IMAP 只记录邮箱内最新 UID，不处理历史邮件（防止旧指令突然生效）；因此**先开启监控，再发送第一封控制邮件**。邮箱重置也重新从最新 UID 开始。
- 不要转发带控制链接的邮件；指令一次有效，使用后不能再重放。令牌 48 小时过期，邮箱出错只记录错误、不自行暂停或恢复。

**部署说明：** 沿用已有 `gpushare-monitor.service`，只需按上述方式配置环境变量并**重启这个监控单独的服务**，不需要更改 Nginx、数据库、域名、其他定时任务或邮件发送程序。

```bash
sudo systemctl restart gpushare-monitor.service
journalctl -u gpushare-monitor.service -n 40 --no-pager
```

当监控已暂停，进程仍然运行但**不查询 GPU 库存、不发送有卡提醒**，这样才能接收后续恢复邮件；如果你想完全停用所有轮询（包括邮箱），仍可运行 `sudo systemctl stop gpushare-monitor.service`。此时邮件中的恢复指令将无法生效，必须先从服务器重新启动。

**测试：** `python3 -m unittest discover -s tests -v` 含 HMAC 签名、48 小时失效、陌生发件人拒绝、历史邮件跳过、IMAP 游标持久化、暂停/恢复与重复命令防重放的离线测试。**尚未连接真实邮箱进行收发联调，需要你在自己的服务器完成 IMAP/SMTP 配置与一次邮件控制演练。**

若不想等到 GPU 有货才演练，可以在**监控服务已经运行且 IMAP 首次初始化成功后**，在导入本地 `.env` 的 shell 中执行：

```bash
python3 gpushare_monitor.py test-control
```

程序将寄出标题包含「邮件暂停控制测试」的模拟控制邮件，**并不声称 GPU 有卡**；你点击其中的控制链接发送预填邮件，检查日志显示 `PAUSED` 并确认收到暂停回执；随后点击回执中的恢复链接发信，日志应继续记录 GPU 检测。