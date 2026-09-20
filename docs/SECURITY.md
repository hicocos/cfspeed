# 安全与备份边界

## 存储

- 管理员密码：随机盐 + PBKDF2-HMAC-SHA256（600000 轮），不保存可逆密码。初始随机密码仅在 `initial-admin-password.txt` 保存供第一次登录（0600），改密后删除。此引导文件是明确的明文例外。
- Web 填写的 Cloudflare Token、DNSPod Secret ID/Key、PushPlus Token：整个 secrets 映射以 AES-256-GCM 加密；每次写入随机 96 位 nonce，版本/配置元数据纳入附加认证数据。文件格式为 admin.json v2。配置域名、服务参数、密码哈希仍可见，但参与完整性认证。
- API 只返回是否已配置，不回显密钥；运行日志不写原始请求/响应、签名和 Cookie。
- 密钥是 32 **原始字节**，不是密码字符串、不是 hex/base64 文本。显式 `CFSPEED_MASTER_KEY_FILE` 不自动创建，文件必须普通非符号链接、0600；丢失、替换、格式错误、密文/认证元数据篡改均拒绝启动或保存，不悄悄清空凭据。
- 建议将独立密钥保存在受控目录，父目录 root-only，文件 UID10001、0600；通过本机 compose.override.yaml 只读挂载到 `/run/secrets/cfspeed-master-key` 并配置 `CFSPEED_MASTER_KEY_FILE`。不要提交该文件或 Compose overlay。
- 未指定显式密钥路径的直接运行/通用演示，首次初始化默认在 state_dir/master.key 创建密钥；这不提供数据卷与密钥分离。正式部署应单独挂载密钥。
- 环境变量/.env 兼容凭据仍为明文，不要误称它们加密。建议正式使用 Web 加密存储，并清除不再需要的环境变量秘密。
- 主机 root、Docker 管理者或已控制服务进程的人可读取密钥与解密后的进程内存；加密保护的是单独泄露的数据文件/备份，不等于主机失陷防护。

## 迁移、提交与恢复

旧 v1 明文 secrets 在启动时原子转换为密文，然后才将凭据交给运行时，不创建额外的明文迁移备份。历史备份和文件系统残留不可能凭这个迁移保证擦除；应按既有备份策略保护/过期处理，不能宣称安全删除。

配置写前创建 `config-update.pending`，保存加密候选与运行状态后清除。保存失败尝试恢复旧密文/配置；无法恢复则停止调度、清会话。进程在提交中途崩溃，下一次启动看到 pending 会**强制预览**再恢复，避免重启自动开启未确认的正式模式。可能需要重新明确启用正式同步。

DNS 修改不是跨记录事务。部分成功不会回滚其他成功记录；写响应丢失仍读取核验，不能读回时持久化 pending_operations 待后续精确核对。停止/取消期间同样保留不确定写入。

备份须分别保护：
1. `cfspeed_cfspeed-data` 卷（含密文、密码哈希、设置、历史）。
2. `/etc/cfspeed/master.key`，存放于分离的受控密码库/离线位置，避免与普通卷备份绑在一起。
3. TOML、.env（若有明文密钥尤其需保护）、本机 Compose overlay。

恢复时先放回原密钥并保持权限/UID，再恢复数据。**不要删除密钥重新生成，不要仅回滚到不理解 v2 的旧镜像。** 真正需要回退旧版须显式恢复此前数据快照并重新评估明文风险；不能让 v1 代码读取 v2 数据。

## HTTP 与会话

- 公网部署需要设置固定 HTTPS Origin/Host 并配置 HTTP 到 HTTPS 跳转；`cfspeed.example.com` 仅为占位域名。
- Cookie：Secure、HttpOnly、SameSite=Strict，内存会话，重启/退出/改密失效。
- 变更要求 Origin + CSRF；会话复核与提交共用撤销锁，已完成的退出不能被迟到请求绕过。
- 登录按可信客户端隔离限额，密码修改独立限额；密码运算并发限制。Nginx 覆盖 X-Real-IP，应用只信任明确 Docker 网关 /32，绝不信任任意客户端的 X-Real-IP。
- 公网 Nginx 有登录/普通请求/连接限流、请求体限制、头/体超时、请求缓冲和无查询串访问日志。应用连接 10 秒绝对预算，避免逐字节请求绕过闲置超时。
- CSP、frame-ancestors/X-Frame-Options、nosniff、no-referrer、HSTS；CSS 因参考组件动态样式保留 unsafe-inline，不开放 inline JavaScript。
- 后端 8788 仅回环映射；不应直接开放到互联网。这些限制适合私人控制台，不能保证抵挡大规模 DDoS。

## 源与网络

来源支持 Web 切换内置接口；HTTPS 不跟随重定向，地址内容严格校验。公网 IPv4 校验不代表来源可信，也不证明属于 Cloudflare 或测速结果新鲜。对来源被攻破的风险需要上游信任或后续专门 allowlist 策略，不能虚称已验证来源真实性。

读取有字节上限、分块读取期限与停止检查，重试等待可取消；单次阻塞 read 仍受 socket timeout，操作系统 DNS 解析不承诺硬实时截止。退出超时时容器仍会强制结束，未确认 DNS 操作由持久化记录保留。

## TLS 运维

使用 `deploy/cfspeed.nginx.example.conf` 作为起点，替换所有占位域名、证书路径、日志路径与 webroot。自行安装证书客户端并配置续期；本仓库不携带生产证书或主机专属续期服务。
