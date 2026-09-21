# Web 使用与部署

## 入口与初始登录

默认仅绑定 `127.0.0.1:8788`，访问 `/` 或 `/admin/login`。用户名固定为 `admin`。首次 `serve` 在状态目录生成随机密码文件 `initial-admin-password.txt`（0600），不会在日志打印密码。

```bash
cd /opt/cfspeed
docker compose exec -T cfspeed python -c "from pathlib import Path; print(Path('/data/initial-admin-password.txt').read_text().strip())"
```

直接运行则读取 TOML 中 `state_dir` 下的同名文件。登录后「账户安全」可修改密码，至少 8 字符。改密注销全部会话、删除初始密码文件。密码只保存带盐 PBKDF2 哈希，Cookie 为 HttpOnly / SameSite=Strict，HTTPS 登录自动加 Secure。

远程访问建议 SSH 转发，不默认暴露管理端口：

```bash
ssh -L 8788:127.0.0.1:8788 用户@服务器
# 在自己电脑打开 http://127.0.0.1:8788
```

如需公网部署，请使用自己的 HTTPS 域名配置反向代理及 `CFSPEED_PUBLIC_ORIGIN`。`CFSPEED_TRUSTED_PROXIES` 仅填写真实反向代理来源，并让代理覆盖 X-Real-IP；网络重建后重新核对。不要开放后端 8788 公网监听。

## 页面与操作

- 总览与统计：真实源 IP、任务状态、下一轮时间、最近执行；复制 IP；手动预览；正式模式下可二次确认立即同步。
- DNS 目标：添加/编辑/移除 Cloudflare 或 DNSPod 目标，多账户凭据变量名。保留已有解析，不自动创建或删除。**保存或移除目标会切回预览**。
- 同步记录：最近 30 轮、模式、计划/已核验数量、错误及逐记录 current → target。
- 独立调度：启动先获取 IP 再执行 DNS；两种任务共用并发锁，各自完成后计算下次执行时间。手动预览/同步先重新获取，再处理 DNS，重置两种周期。来源失败或来源/最大 IP 数量变更立即废弃当前进程缓存，保留历史地址仅供展示，成功重新获取后才允许 DNS；重启不信任磁盘 IP。
- 状态：`next_source_run_at` 为获取时间，`next_dns_run_at` 为 DNS 时间，兼容字段 `next_run_at` 仍指 DNS。`source_valid` 表示当前进程缓存是否有效，`source_ready` 检查独立来源时效。`/readyz` 还要求本进程成功 DNS 任务、当前模式匹配、无待核对写入，时效分别按各自周期的两倍（至少 120 秒）。文本接口继续保留旧 IP，未就绪时返回 `X-CFSPEED-Ready: false`。
- 历史：最近 30 轮包含 `kind=source/dns/manual`，获取不覆盖上次 DNS 结果、不发送通知，也不清除待核对写入。配置热保存按各任务上次结束重新计算 deadline；来源切换令获取立即到期，DNS 不被强制提前。
- 服务设置：独立的 IP 获取间隔（分钟）与 DNS 同步间隔（小时）、读取重试、超时、最大 IP 数量、预览/正式模式。PushPlus 配置区块已移除；保存此页不会修改后台兼容通知配置或凭据。
- 账户安全：更新密码并撤销现有会话。
- 使用文档：部署、验证与 DNS 写入边界。

IP 来源可在 Web 选择原有 `https://ip.164746.xyz/ipTop.html` 或 `https://ip.v2too.top/api/nodes`。后者分别请求 `?carrier=ct`、`?carrier=cm`、`?carrier=cu`，各取数组第一项 IP 后合并去重；不重排、不用后续项补位，任一接口异常则整轮失败并保留原解析。切换来源默认回到预览，选择保存后持久化，无需重启。没有测速功能。只有明确切换正式模式、输入 `启用同步` 并提交确认后，服务才会按周期修改真实 DNS；已启用正式同步后，仅调整时间、超时或重试次数无需重复输入确认。保存设置不会主动启动新任务，但周期已到时任务可能紧接着执行。正式接管前停用 GitHub 上旧 DNS 定时任务。

手动预览即使在正式调度模式下也不写 DNS，但**不会关闭正式调度**。概览运行模式始终显示已配置的调度模式，单轮历史单独标注 preview/apply；一次预览不能证明正式任务就绪。

## 配置优先级与秘密

- 首次启动用 TOML 初始服务参数/目标初始化私有 `admin.json`。
- 之后 Web 保存的业务配置与目标优先，包括 IP 来源和同步周期；TOML 继续决定监听地址/端口、状态目录。
- Web 保存凭据优先于相同名称的环境变量。密码输入留空保留已有值，读取配置只返回 configured 布尔值，不回显秘密。
- DNS/通知凭据以 **AES-256-GCM** 加密保存在 `admin.json` v2（0600），元数据参与认证，篡改会拒绝启动。正式部署建议将独立密钥只读挂载到容器 `/run/secrets/cfspeed-master-key`，并设置 `CFSPEED_MASTER_KEY_FILE`，不要放入数据卷或镜像。管理员密码使用带盐 PBKDF2 哈希。root / Docker 管理者若同时取得密钥和数据仍能解密，这不是抵抗主机失陷的方案。环境变量兼容凭据和首次密码文件并不因此自动加密。详见 [安全与备份](SECURITY.md)。
- 冲突配置 revision 返回 409，需要重新加载再修改；同步进行中保存也返回 409，不会与任务竞争。
- 未完成 DNS 写入另存 pending_operations，重启、历史轮转、编辑目标都不会悄悄删除。
- 维护状态和秘密的命名卷是 `cfspeed_cfspeed-data`。不要执行 `down -v`。

`CFSPEED_TEST_ADMIN_PASSWORD` 只为隔离测试提供确定性启动，不要在正式部署中设置。初始密码遗失后不要删除 `admin.json`（包含业务配置和凭据）；该初版未提供无验证密码重置 API。

## API 与保护

公开：`GET /healthz`、`GET /readyz`、`GET /ipTop.html`、`GET /ipTop10.html`、前端静态资源。

- `GET /api/auth/session`：authenticated / username / csrf。
- `POST /api/auth/login`：username / password。
- `POST /api/auth/logout`：空对象。
- `POST /api/auth/password`：current_password / new_password。
- `GET /api/admin/status`（旧 `/api/status` 同样需要登录）。
- `GET /api/admin/config`：revision / service / targets / credentials configured。
- `PATCH /api/admin/config`：revision + 部分 service/targets/secrets；关闭预览需要 confirm_apply=true。
- `POST /api/admin/run`：dry_run=true 预览；正式执行需要已配置正式模式，dry_run=false + confirm_apply=true。

所有变更要求同源 Origin；登录以外还要求 Cookie 与 `X-CSRF-Token`。登录限流、请求大小/连接数量限制、静态路径白名单、同源 CSP。初版用于小型私人控制台，不是多租户平台。

## 构建与验收

Docker 多阶段构建：Node 22 / pnpm 11.9.0 编译 Vue，然后只将产物与 Python 源码放进非 root 运行镜像。运行时不需要 Node，也不访问 GitHub。直接运行 Web 需要先 `pnpm --dir web install --frozen-lockfile && pnpm --dir web build`；静态目录默认 `web/dist`，可用 `CFSPEED_WEB_ROOT` 指定。

```bash
./.venv/bin/python -m unittest discover -s tests -q
pnpm --dir web build
python3 tests/smoke_live.py
```

`tests/web_e2e.py` 对独立本机测试服务进行真实浏览器增删改、设置保存、登录改密、手动预览；必须传入测试数据目录的密码文件，**不可直接拿生产状态跑此测试**。

前端复用来源与清单见 `WEB-REFERENCE.md` / `reference-files.json`。部署截图和报告不随源码发布。

概览中的「刷新 IP」只获取来源，不执行 DNS 同步、不发送通知，也不改变 DNS 同步时间。失败时保留历史 IP 用于展示，但暂停使用这些地址写入 DNS，直到获取成功。
