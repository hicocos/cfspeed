# cfspeed

定时获取优选 IP，自动更新 Cloudflare 或 DNSPod 的域名解析。通过网页管理 DNS 目标、同步周期和执行记录，无需 GitHub Actions。

> 本项目不测速，只读取第三方 IP 接口。仅更新已有的 IPv4（A）记录，不创建或删除解析记录。

## 功能

- **多目标同步**：支持 Cloudflare、腾讯云 DNSPod，可配置多个域名和账户。
- **切换 IP 来源**：支持 `ip.164746.xyz` 和 `ip.v2too.top`。
- **先预览再启用**：查看计划修改的记录，确认后开启定时同步。
- **查看执行结果**：记录每轮同步结果，更新后重新查询 DNS 服务商核对。
- **网页管理**：设置同步间隔、维护凭据、修改管理员密码。

## 快速部署

需要安装 **Git、Docker 和 Docker Compose v2 或以上版本**。以下命令在部署机器上执行；首次构建需要联网下载镜像和依赖，不必另装 Python 或 Node.js。

### 1. 下载并启动

```bash
git clone https://github.com/hicocos/cfspeed.git
cd cfspeed
cp config.docker.example.toml config.docker.toml
cp .env.example .env
chmod 600 .env
docker compose up -d --build --wait --wait-timeout 120
```

首次部署无需填写 DNS 凭据，稍后在网页中添加。默认每 6 小时运行一次，启动时也会运行；初始为预览模式，不修改 DNS。

如果启动失败，运行 `docker compose logs --tail=100 cfspeed` 查看原因。上面的复制命令仅用于首次安装，不要覆盖已有配置。

### 2. 打开管理页面

- **部署在自己的电脑上**：打开 <http://127.0.0.1:8788/admin/login>。
- **部署在远程服务器上**：在自己的电脑新开终端，建立 SSH 隧道，再打开同一地址。

```bash
# 替换 SSH 用户名和服务器地址，保持此终端打开
ssh -N -L 8788:127.0.0.1:8788 用户名@服务器地址
```

默认只监听服务器本机，不能用 `http://服务器IP:8788` 直接访问。如需域名访问，见 [HTTPS 部署](docs/DEPLOYMENT.md#https-访问)。

### 3. 获取密码并登录

在部署机器的 `cfspeed` 目录执行：

```bash
docker compose exec -T cfspeed python -c "from pathlib import Path; print(Path('/data/initial-admin-password.txt').read_text().strip())"
```

用户名为 **`admin`**，密码为命令输出。登录后进入「账户安全」修改密码。初始密码不会出现在日志中，改密后初始密码文件会被删除。

### 4. 添加 DNS 目标

先在 DNS 服务商控制台创建需要同步的 **A 记录**，再进入「DNS 目标 → 添加目标」。

**Cloudflare**

- 区域 ID：域名对应的 Zone ID。
- 解析名称：完整域名，例如 `cf.example.com`。
- API Token：仅授权目标域名的 DNS 编辑权限；不支持 Global API Key。

**DNSPod（腾讯云）**

- 所属域名：例如 `example.com`。
- 解析名称：例如 `cf`；根域名填写 `@`。
- 线路 ID：默认线路为 `0`，其他线路填写对应 ID。
- 凭据：腾讯云 `SecretId` 和 `SecretKey`，需具备查询和修改目标域名记录的权限。

凭据可直接在网页填写，无需修改 `.env`。多账户使用不同的凭据变量名，避免覆盖其他目标的凭据。

### 5. 预览并启用同步

1. 在「服务设置」选择 IP 来源和运行间隔，保持预览模式并保存。
2. 在「总览与统计」点击「立即预览」。
3. 在「同步记录」查看详情，确认域名、现有 IP 和目标 IP 正确。
4. 回到「服务设置」，将运行模式改为「正式同步」，保存并按提示输入 `启用同步` 确认。

启用后将按周期更新 DNS；正式模式下重启服务也会执行同步。修改 DNS 目标或切换 IP 来源会回到预览模式，需要重新确认启用。请先停用操作相同记录的其他定时任务，避免重复修改。

## 更新与停止

在项目目录执行。更新前备份数据；不要重新复制示例配置。

```bash
# 更新源码并重新构建
git pull --ff-only
docker compose up -d --build --wait --wait-timeout 120

# 查看状态
docker compose ps

# 停止服务，保留数据
docker compose down
```

配置、凭据和历史记录保存在 Docker 数据卷中，默认卷名为 `cfspeed_cfspeed-data`。**不要使用 `docker compose down -v`，它会删除数据。** 备份时需保留加密密钥，详见 [安全与备份](docs/SECURITY.md)。

## 使用须知

- **IP 不够时**：只更新部分已有记录，其余保持原值，不删除。预览和正式同步选中的记录可能不同。
- **来源失败时**：不使用旧 IP 继续更新 DNS。第三方 IP 的速度和可用性由来源决定，本项目不保证测速质量。
- **部分更新失败时**：已经成功的修改不会自动回滚，请在同步记录中检查失败或待核对项。
- **保留记录设置**：不主动改变 TTL、Cloudflare 代理开关或 DNSPod 线路。需要 DNS-only 时，请在 Cloudflare 自行关闭代理。
- **三网来源**：分别取电信、移动、联通的首个 IP 并合并去重，不会自动把 IP 分配到对应运营商线路。
- **IP 文本接口**：`/ipTop.html` 返回最近一次成功获取的 IP，`/ipTop10.html` 是同内容别名，不保证十条。来源失败后可能仍显示旧值，可结合 `/readyz` 检查任务状态。

## 更多文档

- [部署说明](docs/DEPLOYMENT.md)：HTTPS、Python 运行方式、健康检查。
- [Web 使用说明](docs/WEB.md)：页面操作、配置优先级和 API。
- [安全与备份](docs/SECURITY.md)：凭据加密、密钥和恢复注意事项。

## 来源与许可

项目源自 [ZhiXuanWang/cf-speed-dns](https://github.com/ZhiXuanWang/cf-speed-dns)，前端复用信息见 [来源说明](docs/WEB-REFERENCE.md)。

本仓库尚未声明开源许可证；公开可见不代表授予任意复用或再分发许可。
