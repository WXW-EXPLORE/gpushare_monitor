# GPUShare Monitor · 只读候补监测

> **开发状态：监控核心、邮件和服务模板已实现并可本地测试；尚未接入经验证的 GPUShare 实时库存源。** 未确认数据源之前不能声称可以监测到空闲 GPU，更不会发送虚假的“有卡”邮件。

只监测你**已经拥有的实例所需的具体物理主机资源**；发现该主机有足够空闲 GPU 时发邮件，**你本人登录 GPUShare 手动启动**。不自动登录、创建、启动、停止、迁移或付款。Python 标准库实现，无 Docker / Playwright / 第三方依赖。

## 当前可行性与关键限制

- 恒源云文档介绍控制台操作，但没有公开确认免登录的“已有实例可启动 GPU 数量”接口。第三方 [GPU 算力雷达](https://rentgpu.link/sources) 报告使用 `api.gpushare.com/app/api/market/matching/machine` **登录态**市场匹配接口获取价格；该端点的请求格式、返回结构、具体机器闲置数量及能否免登录，**均未实测确认**，不可直接拿它作为生产库存源。
- 同型号 GPU 在**其他主机**有库存，不代表你的已有实例能启动。程序必须精确匹配 `machine_id` 并比对所需卡数；机器缺失、数据缺字段、登录过期、错误与接口异常统一判定为 **UNKNOWN**，绝不判为可用。
- 本程序不提供任何写入 GPUShare 的 API。只有你明确核实数据源是只读操作后才能启用；程序强制域名白名单和 HTTPS，并阻止常见的开机/购买/删除等操作路径。

## 目标机器（根据用户截图）

已在 `config.my-instance.example.json` 预填目标资源（不含账号、Cookie、邮箱凭据）：

| 字段 | 值 |
| --- | --- |
| 地区 | 西北 |
| 目标 GPU | 4080s-32G × 1 |
| 机器 ID | `MACHL7cYdJ32V4WRGwRGsbSN` |
| 实例 ID | `I2ba996131d00a01558` |
| 截图价格 | ¥1.80/小时（仅当时显示价） |

**请在控制台核对机器 ID 的大小写。** 机器 ID 与实例 ID 不同：库存检查需要匹配物理主机 ID，不是 GPU 型号的市场总库存。截图的“无空闲显卡”仅说明截图时状态，不能作持续监控数据源。

**重要：** 截图中 /hy-tmp 数据保留倒计时已不足 55 小时；实例到期日不代表临时盘保留期限。请及时备份重要科研数据。

## 1. 部署预备

```bash
git clone https://github.com/WXW-EXPLORE/gpushare_monitor.git
cd gpushare_monitor
cp config.my-instance.example.json config.json
cp .env.example .env
chmod 600 config.json .env
python3 -m unittest discover -s tests -v
```

编辑 `.env` 填入 SMTP 邮箱、SMTP 授权码。邮箱授权码**不是网页登录密码**。QQ 邮箱一般使用 `smtp.qq.com:465`；其他邮箱按服务商设置，支持 465/SSL 及 STARTTLS。不要提交 `.env`、`config.json`、Cookie 或令牌。

```bash
# 导入仅当前 shell 有效的邮箱变量，不要用 shell 调试日志输出凭据
set -a; . ./.env; set +a
python3 gpushare_monitor.py test-mail
```

## 2. 连接真正的只读 GPU 库存来源（必须完成）

在浏览器手动登录 GPUShare（**仅用于你手工核对数据源**）。打开开发者工具 → Network → Fetch/XHR，找到“我的实例”或“云市场”显示**与原实例相同物理主机 ID、显卡型号、空闲卡数**的只读请求。确认请求只查询数据、不引发状态变化，且使用符合平台规则的访问方式。**不要复制开始/停止/购买等操作请求**。

核对该请求的：请求 URL、GET 或只读 POST、必要的查询 JSON、返回 JSON 的字段结构，以及是否需要登录态。

可以将**手动去除手机号、姓名、Cookie、Token 等敏感字段后的 JSON 响应**保存为本机 `sample.json`，运行以下命令离线检查是否包含目标机器 ID：

```bash
python3 gpushare_monitor.py inspect-json --config config.json --sample-file sample.json
```

此命令**不联网、不发邮件**，只打印匹配结构路径和疑似库存字段名称，不输出字段值。发现匹配仍不能证明该机器可启动，必须人工核对字段语义。排查结束后请删除样本文件，切勿提交仓库。将这些映射进 `config.json`，例如**下面仅为假想结构，不是 GPUShare 已验证接口**：

```json
{
  "target": {"machine_id": "YOUR_EXACT_HOST_ID", "gpu_model": "RTX 4090", "gpu_count": 2, "console_url": "https://gpushare.com/"},
  "poll_seconds": 60,
  "source": {
    "confirmed_read_only": true,
    "url": "https://api.gpushare.com/YOUR_VERIFIED_READ_ONLY_PATH",
    "method": "GET",
    "headers": {},
    "records_path": "data.machines",
    "id_path": "id",
    "model_path": "gpu_model",
    "free_path": "free_gpu_count"
  }
}
```

例子中若服务器返回 `{"data":{"machines":[{"id":"YOUR_EXACT_HOST_ID","gpu_model":"RTX 4090","free_gpu_count":2}]}}`，则判定 **AVAILABLE**。若业务接口以 POST 查询，改 `method` 为 `POST`，填写 `json_body`，但**只允许经确认的只读查询**。如 API 返回 `success=false`，可选配置 `success_path`/`success_value` 来强制验证成功状态。实际 JSON 路径必须与返回数据严格一致。

若只读接口确实需要会话，可由你在浏览器自行登录；监控程序**不帮你登录**。在确保平台允许的前提下，手动将必要的只读 Cookie 放到本机 `.env`：

```bash
# .env 内容（示意）：
GPUSHARE_READ_COOKIE='YOUR_SESSION_COOKIE'
```

并在 `config.json` 的 `source.headers` 设置 `"Cookie": "env:GPUSHARE_READ_COOKIE"`，令牌始终仅从环境变量读取。**不要把 Cookie、完整请求/响应（可能含隐私信息）发到公开聊天、Issue 或 GitHub。** 如果完全不想向后台程序提供登录态，那就只能使用经过验证的免登录库存来源；查不到这样的来源时，本程序不能提供真正的实时提醒。

## 3. 校验并启动

```bash
# 只读查询一次；不发通知、不改状态
python3 gpushare_monitor.py check --config config.json
# 只检测一次，满足条件才发送一封邮件
python3 gpushare_monitor.py once --config config.json
# 后台循环（先在前台观察日志）
python3 gpushare_monitor.py monitor --config config.json
```

默认每 60–70 秒检查（增加抖动以降低固定频率请求）；UNKNOWN 状态逐步退避，最长 15 分钟。首次检测到有卡就邮件，后续库存持续为正不重复；至少两次明确无卡后，重新进入有卡状态才再次提醒。状态文件 `state/state.json` 会原子写入，配合文件锁防止多进程重复通知。连续 5 次探测失败可每 6 小时发一次异常提示。

## 4. systemd 部署（单独服务，不碰现有业务）

复制 `systemd/gpushare-monitor.service.example` 到本地 `/tmp/gpushare-monitor.service`，把 `REPLACE_USER` 替换为运行用户，`REPLACE_PROJECT_PATH` 替换为完整绝对目录（例如 `/home/admin/gpushare_monitor`）。确认 Python 路径及 `.env` 内容，然后：

```bash
sudo cp /tmp/gpushare-monitor.service /etc/systemd/system/gpushare-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now gpushare-monitor.service
systemctl status gpushare-monitor.service
journalctl -u gpushare-monitor.service -f
# 随时停止：sudo systemctl stop gpushare-monitor.service
```

此服务仅新建自己的 systemd 单元，不要求重启 Nginx、Docker、邮件服务或其他服务。**部署前先用 check 和 test-mail 验证**。登录态过期、接口变更需要你手动更新凭据或配置。

## 5. 验收

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile gpushare_monitor.py
```

测试覆盖精确机器匹配、数量要求、数据缺失/异常、去重与重新触发、邮件失败后重试、敏感信息环境变量、状态持久化。测试只用合成响应，不对真实平台发请求。

**安全提醒：** 按官方文档，停止的实例满 10 天可能被自动释放；`/hy-tmp` 在停机 24 小时后也可能清除数据，请提前备份重要研究文件。

## 补充安全与现状说明

- 当前只建立监控核心和目标匹配配置，**未确认免登录的真实实时库存接口，不应部署后台循环并期待收到真实有卡邮件**。
- HTTP 禁止重定向，以免会话请求头被送到其他站点。程序不自动登录，也不绕过验证码或限制。
- 一个仅提供 GPU 型号、价格的第三方数据源不足以证明上述**指定机器 ID**出现空闲资源。无法确定时只报 UNKNOWN。
