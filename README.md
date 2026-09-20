# cfspeed · 独立 DNS 同步控制台

从现有优选 IP 接口定时获取地址，更新 Cloudflare / DNSPod 已有 A 记录。**不执行测速，不需要 GitHub Actions、GitHub Pages 或上游仓库同步。**

管理端「服务设置 → 优选 IP 来源」支持原有来源和 `ip.v2too.top`。选择后者会分别读取 `https://ip.v2too.top/api/nodes?carrier=ct`、`?carrier=cm`、`?carrier=cu` 的第一项 IP，按电信、移动、联通顺序合并去重。任一接口失败或首项无效，整轮不更新 DNS；不会使用后续排名补位。来源切换默认回到预览模式，保存后生效。此列表沿用现有 DNS 分配逻辑，不自动创建运营商线路或新增 A 记录。

```text
https://ip.164746.xyz/ipTop.html → IPv4 校验去重 → 读取现有记录 → 预览 / 更新 → API 读回核验
```

Python 3.11+ 后端使用 cryptography 进行凭据认证加密；Web 使用 Vue 3 + TypeScript + Vite，样式与组件复用 random-image-api 控制台。支持 Docker Compose 或直接运行 Python；后者不是单文件二进制。

## Docker 快速运行（默认安全预览）

在项目目录内：

```bash
cp config.docker.example.toml config.docker.toml
cp .env.example .env
chmod 600 .env
docker compose up -d --build
docker compose logs -f
```

已有配置时不要重复复制覆盖。默认每 6 小时（21600 秒）运行一次，可在服务设置中按小时（h）修改，支持小数且必须对应整数秒（范围仍为 30–604800 秒），启动时立即执行一次；周期从上一轮结束开始计算。默认没有 DNS 目标，仅获取真实 IP 并持久化。预览模式即使配置了目标，也只读取 DNS 并展示变更，不提交修改或通知。

默认仅绑定本机。公网部署请配置自己的 HTTPS 域名及 `CFSPEED_PUBLIC_ORIGIN`；`deploy/cfspeed.nginx.example.conf` 仅为示例，使用前替换域名、证书和日志路径。

访问：

- `http://127.0.0.1:8788/`：Web 管理控制台，未登录跳转管理员登录。
- `/api/admin/status` / `/api/status`：当前 IP、执行模式、目标结果、最近 30 次运行记录；均需要登录。
- `/ipTop.html`：最近一次成功获取的 IP，逗号分隔纯文本。
- `/ipTop10.html`：兼容别名，同一份来源数据，**不保证十条、不伪造排名或测速结果**。
- `/healthz`：进程 HTTP 存活；供 Docker healthcheck 使用。
- `/readyz`：最近一次完整任务成功且未过期时返回 200；失败、尚未运行、过久没有成功时返回 503。预览模式的 ready 只代表预览成功，不等于 DNS 写入验证通过。

```bash
curl --fail http://127.0.0.1:8788/readyz
curl --fail http://127.0.0.1:8788/ipTop.html
docker compose stop
```

容器非 root、只读根文件系统、128MB 内存限制、日志轮转；状态保存在命名卷 `cfspeed_cfspeed-data`。不要随意 `docker compose down -v`（会删除历史状态）。应用只映射本地 8788 端口。如需公网访问，请自行配置 HTTPS 反向代理，勿直接开放 8788 端口。

## Web 登录与使用

用户名 `admin`，首次启动自动生成随机密码，不在日志中输出。读取初始密码：

```bash
docker compose exec -T cfspeed python -c "from pathlib import Path; print(Path('/data/initial-admin-password.txt').read_text().strip())"
```

登录后进入「账户安全」修改密码；改密会注销所有会话并删除初始密码文件。完整说明见 [Web 使用与部署](docs/WEB.md)。

## 直接运行

```bash
cp config.example.toml config.toml
# 直接运行 Web 需先安装 Node.js 22+ / pnpm，再构建一次
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python3 -m cfspeed check --config config.toml
python3 -m cfspeed run --config config.toml
python3 -m cfspeed serve --config config.toml
```

- `check`：检查配置及所需环境变量，不联网。
- `run`：执行一次，主任务失败返回非零退出码。
- `serve`：定时任务 + Web 管理控制台。
- `--dry-run`：强制不写 DNS。
- `--state-dir PATH`：覆盖状态目录。相同状态目录用进程锁防止并行执行。

直接运行不会自动读取 `.env`。配置目标前在启动环境中设置凭据；或者使用 systemd 的 EnvironmentFile。提供 `deploy/cfspeed.service` 模板，需 Python 3.11+、系统用户 `cfspeed`、程序位于 `/opt/cfspeed`、配置 `/etc/cfspeed.toml`、凭据 `/etc/cfspeed.env`（建议 root:root 0600）。模板**没有自动安装或启用**。

## 配置 DNS 目标

首次启动前可把目标加入 `config.docker.toml`（直接运行则加入 `config.toml`），启动后建议在 Web「DNS 目标」中维护。Web 保存的设置与目标优先于 TOML，来源 URL 可在 Web 中选择并持久化，监听地址与数据目录仍由 TOML 决定。支持多个目标和凭据，不自动扫描账户，不创建或删除记录。

Cloudflare 示例（占位 Zone ID 必须替换）：

```toml
[[targets]]
label = "Cloudflare 主域名"
provider = "cloudflare"
zone_id = "REPLACE_WITH_YOUR_32_HEX_ZONE_ID"
name = "cf.example.com"
token_env = "CF_API_TOKEN"
```

在 `.env` 填写 `CF_API_TOKEN`，使用仅对目标 Zone 授权 DNS 编辑的 API Token。Zone ID 要求完整小写 32 位十六进制；不会猜测或修复无效标识。`name` 为完整小写 ASCII 域名，国际化域名使用 punycode。

DNSPod 示例（腾讯云中国站接口）：

```toml
[[targets]]
label = "DNSPod 默认线路"
provider = "dnspod"
domain = "example.com"
name = "cf"
line_id = "0"
secret_id_env = "DNSPOD_SECRET_ID"
secret_key_env = "DNSPOD_SECRET_KEY"
```

在 `.env` 填写 `DNSPOD_SECRET_ID` / `DNSPOD_SECRET_KEY`。根域名 `name="@"`；线路按 ID 精确匹配，不依赖中文/英文线路名称。建议专用最小权限子账户：目标域名的 DescribeRecordList、DescribeRecord、ModifyRecord。暂不支持腾讯云临时安全令牌及国际站端点。

配置多个账户时，可以为每个目标指定不同的环境变量名；Compose 的 `.env` 对应变量也一并添加。

首次部署保持 `[service] dry_run = true`；已有管理配置时，在 Web 中确认预览模式：

```bash
docker compose up -d --force-recreate
curl --fail http://127.0.0.1:8788/ipTop.html
```

在 Web「同步记录」检查每条 `current → target`。确认后在「服务设置」切换正式同步并二次确认；**正式模式按周期修改 DNS，重启会立即执行一轮**。已有 admin.json 时不通过改 TOML 覆盖 Web 模式。此操作应由账户持有人明确决定。

## 更新策略与失败保护

- 仅支持公网 IPv4 → 已有 A 记录。源为空、私网、IPv6、非法文本、HTML 或超限时，整次拒绝；不拿旧缓存继续写 DNS。
- IP 足够时按上游原始顺序取前 N 个 IP，N 为该目标已有 A 记录数。尽量保留已经正确的记录分配，上游顺序变化但集合不变不会重复写。
- IP 不足时仅同步部分已有记录，其余保留旧值继续解析，不删除；预览与正式同步所选记录可能不同。
- 不自动创建记录。无记录、暂停记录、分页计数不一致、目标不匹配都会报错。
- 保留 Cloudflare TTL、代理开关、备注、标签、settings；保留 DNSPod TTL、线路、权重、启用状态。Cloudflare 优选用途通常需要 DNS-only，本程序不会擅自切换代理。
- 更新前再次读取检查并发变化；更新后按确切记录 ID 读取核验。与其他 DNS 管理器之间仍无原子 CAS，正式运行时应避免其他工具同时修改这些记录。
- 查询类请求有有限重试；写请求不自动重试。写超时或返回异常后仍尝试读回：目标状态一致则记录警告并确认成功，否则标记 `failed_or_unverified`，实际可能已写入。
- 多目标或多记录**不是事务**。一部分成功、一部分失败时保留已经核验的修改，不自动回滚；状态明确记录每条结果。
- HTTP/接口异常和日志不包含凭据、原始响应体或签名。更新前持久化写入意图；进程崩溃后将该轮归档为 interrupted。独立 `pending_operations` 不随历史轮转丢失，只有成功读到相同作用域、相同记录 ID 的当前状态后才解除，并记录所观察的值；这不证明原请求确实成功。目标被移出配置或记录被删除时会继续保留，需人工核对。
- `/readyz` 要求本次进程至少成功一轮且没有未核对写入；重启或切换配置后不会继承历史就绪状态。
- 源失败时 `/ipTop.html` 继续展示上次成功值，`X-CFSPEED-Ready: false` 与 `/readyz` 表示当前不健康。数据状态包含获取时间；**来源没有测速生成时间，不能证明其本身最新或质量可靠**。
- 最多保留 30 轮历史（另受大小上限约束）。管理页面与配置/详细状态 API 需要管理员会话；IP 文本、healthz/readyz 保持公开。默认仅绑定本机，远程查看请使用 SSH 隧道；若发布公网，使用 HTTPS 反向代理并保留原 Host。

## 可选通知

在 `[service]` 添加 `pushplus_token_env = "PUSHPLUS_TOKEN"` 并设置环境变量。仅正式模式有已核验修改或同步失败时通过 HTTPS 请求 PushPlus；预览不发送。API 接受标记 `accepted`，不代表已验证微信收件成功。通知失败独立显示，不伪报 DNS 同步失败。

## 测试与目录

```bash
python3 -m unittest discover -s tests -v
# 可选：真实 IP 源、两次定时运行、SIGTERM 正常退出；不操作 DNS
python3 tests/smoke_live.py
```

- `cfspeed/`：新独立程序。
- `tests/`：离线单元与本机 HTTP/持久化/调度回归，提供商测试明确使用模拟响应，不代表生产 API 写入已验证。
- `compose.yaml` / `Dockerfile`：无 GitHub 运行时依赖，构建使用 Docker 官方 Node/Python 镜像（固定 digest）及 npm/pnpm 包，运行时不依赖 GitHub。

## 发布与来源

本仓库是当前独立服务的源码快照，不包含旧 Git 历史、旧 Actions、运行数据、真实配置、密钥、备份或部署验收截图。

历史项目来源为 [ZhiXuanWang/cf-speed-dns](https://github.com/ZhiXuanWang/cf-speed-dns)。旧脚本和静态结果未包含在本次发布中。前端复用来源见 [WEB-REFERENCE.md](docs/WEB-REFERENCE.md)。原项目未见 LICENSE，本仓库未擅自添加开源许可；公开可见不等于授予任意复用或再分发许可。

若从旧系统迁移，启用正式同步前请自行停用旧 DNS 定时任务，避免双写。本仓库不包含自动执行的 GitHub Actions。
