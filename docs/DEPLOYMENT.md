# 部署说明

首次安装推荐使用 [README 中的 Docker 部署步骤](../README.md#快速部署)。本页介绍其他部署方式和排查方法。

## HTTPS 访问

默认的 Docker 配置只开放 `127.0.0.1:8788`。SSH 隧道适合个人访问；需要通过域名访问时，请在服务器上配置 HTTPS 反向代理。

1. 将自己的域名解析到部署服务器，并申请有效的 TLS 证书。
2. 在项目的 `.env` 中设置 `CFSPEED_PUBLIC_ORIGIN=https://你的域名`，不要带路径。
3. 让反向代理转发到 `http://127.0.0.1:8788`，保留正确的 Host，并设置 `X-Forwarded-Proto: https`。
4. 运行 `docker compose up -d --wait --wait-timeout 120`，让环境变量变更生效。
5. 使用配置的 HTTPS 域名登录，不再通过 HTTP 地址登录。

[Nginx 示例](../deploy/cfspeed.nginx.example.conf) 适用于 Nginx 安装在 Docker 宿主机的情况。它不是一键安装脚本：使用前需替换域名、证书、日志和 ACME 验证目录，并检查 Nginx 版本是否支持其中的指令。不要直接覆盖已有站点配置。

如果需要按访客真实 IP 限流，代理必须覆盖客户端传入的 `X-Real-IP`，并在 `.env` 中用 `CFSPEED_TRUSTED_PROXIES` 指定后端实际看到的代理来源 IP。Docker 网络下该地址未必是 `127.0.0.1`；不要填 `0.0.0.0/0` 或信任所有来源。

## 不使用 Docker

需要 Linux、Python 3.11+、Node.js 22.12+（或兼容的更新版本）及 pnpm 11.9.0。Python 需要提供 `venv` 模块。依赖安装和前端构建需要联网。

```bash
git clone https://github.com/hicocos/cfspeed.git
cd cfspeed
cp config.example.toml config.toml

npm install --global pnpm@11.9.0
pnpm --dir web install --frozen-lockfile
pnpm --dir web build

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m cfspeed check --config config.toml
python -m cfspeed serve --config config.toml
```

`serve` 在前台运行，按 Ctrl+C 停止。默认访问地址同 Docker 部署；远程服务器也需要 SSH 隧道或 HTTPS 代理。

在项目目录另开终端读取初始密码：

```bash
python3 -c "from pathlib import Path; print(Path('data/initial-admin-password.txt').read_text().strip())"
```

以上密码路径对应默认的 `state_dir = "data"`；修改过数据目录时请使用自己的路径。登录后的操作见 [README](../README.md#4-添加-dns-目标)。

直接运行不会自动加载 `.env`。推荐在网页中填写 DNS 凭据；通过环境变量配置时，必须先将变量传给程序运行环境。

需要开机自启时，可按自己的安装目录和系统用户调整 [systemd 模板](../deploy/cfspeed.service)。该模板不会自动创建用户、安装程序或生成配置，请勿未经调整直接启用。

### 命令说明

- `check`：检查配置和所需凭据，不发起网络请求。
- `run`：只执行一轮任务，失败时返回非零退出码。
- `serve`：启动网页管理和定时任务。
- `--dry-run`：强制预览，不修改 DNS，也不发送通知。
- `--state-dir PATH`：指定数据目录。同一目录不能同时运行多个实例。

不要在正在运行的服务旁边使用相同数据目录执行 `run`；需要手动执行时使用网页按钮。

## 健康检查与排查

```bash
docker compose ps
docker compose logs --tail=100 cfspeed
curl --fail http://127.0.0.1:8788/healthz
curl --fail http://127.0.0.1:8788/readyz
```

- `/healthz` 返回 200：HTTP 服务存活，也是 Compose 等待启动时检查的条件。
- `/readyz` 返回 200：本次进程已有近期成功任务，且没有未核对的 DNS 写入。预览成功不代表正式 DNS 更新已经验证。
- `/readyz` 返回 503：可能是首次任务尚未完成、来源不可用、同步失败或存在待核对记录，请登录查看详情。

设置固定 HTTPS 域名后，本机检查需传入匹配的 Host，例如 `curl -H 'Host: cfspeed.example.com' http://127.0.0.1:8788/readyz`，请替换为自己的域名。

常见问题：

- **用服务器 IP 打不开页面**：默认端口仅本机可用；域名访问请参考本页的 HTTPS 配置。
- **找不到初始密码文件**：先确认容器成功启动；修改密码后文件会被删除，不能再用它找回密码。不要删除 `admin.json`，它还包含业务配置和凭据。
- **修改 TOML 后网页配置没变**：首次启动后，网页保存的业务配置优先。来源、同步周期和目标请在网页修改；监听地址和数据目录仍由 TOML 决定。
- **凭据解密失败**：检查原密钥是否保留、挂载和权限是否正确，不要生成新密钥替换旧密钥。

## 开发验证

先安装 Python 依赖和前端依赖，然后在项目目录执行：

```bash
.venv/bin/python -m unittest discover -s tests -q
pnpm --dir web build
```

可选执行 `.venv/bin/python tests/smoke_live.py`：读取真实 IP 来源，在隔离数据目录运行两轮预览，不操作 DNS。

`tests/web_e2e.py` 会修改测试实例的账户和配置，只能针对隔离测试服务运行，不能用于已有业务数据的实例。
