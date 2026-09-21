# Web port mapping / contract

Reference: random-image-api frontend; target: cfspeed frontend.

## Presentation mapping
- AdminLayout.vue → same admin-sidebar / admin-topbar / admin-main / footer, brand changed to cfspeed; links become 概览、DNS 目标、同步记录、服务设置、账户安全、使用文档.
- Stats.vue → same OVERVIEW heading, refresh, 2×2 stats, setup-banner, card sections. Data: fetched IPs, targets, cumulative runs, pending operations (no fake DNS success counters).
- Storages.vue list/editor + Modal.vue → DNS target list and single-target modal (Cloudflare / DNSPod, environment-variable-backed credentials).
- Jobs.vue → run history table, status filter, record outcome detail dialog.
- Settings.vue → same settings-layout/cards and field/button primitives for schedule/safety options.
- Login.vue → same login-page/login-card/brand shell with actual administrator authentication.
- Account security → current/new password, revoke sessions on password change.
- Copy byte-for-byte: style.css, design.css, compact-admin.css, mkw-public.css, mkw-admin.css; Icon.vue, Modal.vue, Notice.vue, EmptyState.vue; two background SVG assets. No image/video/gallery/business APIs copied.
- Reuse exact Geist Variable, Phosphor Vue icon library, Vue and Vue Router versions. Preserve Chinese light theme; reference has no active dark-mode switch, do not invent one.

## Management API contract
All JSON. Error: {error:string}. Auth session cookie; POST/PATCH require matching Origin and X-CSRF-Token (except login uses Origin only). Same-origin browser requests.
- GET /api/auth/session → {authenticated:bool,username:string,csrf:string}
- POST /api/auth/login {username,password} → same session view
- POST /api/auth/logout {} → {ok:true}
- POST /api/auth/password {current_password,new_password} → {ok:true}; clears sessions, frontend returns to login
- GET /api/admin/status → existing State snapshot plus ready:boolean, dry_run:boolean. Existing /api/status can remain local-compatible read-only; never include credentials.
- Web IP 获取间隔使用分钟（min），默认 360 min；DNS 同步间隔使用小时（h），默认 6 h。均允许对应整数秒的小数，显示与保存时分别按 60 / 3600 换算。API/TOML 的 interval_seconds 仍为 DNS 周期，source_interval_seconds 为独立获取周期，均为整数秒（30–604800）；旧配置缺少新字段时继承原间隔。
- GET /api/admin/config → {revision:int,service:{source_url,interval_seconds,source_interval_seconds,timeout_seconds,attempts,dry_run,max_ips,pushplus_token_env},targets:[Target fields],credentials:[{name:string,configured:bool}]}
- PATCH /api/admin/config {revision:int,service?:partial same fields,targets?:Target[],secrets?:{ENV_NAME:string|null},confirm_apply?:bool} → same config view. Scoped merge; stale revision 409. source_url accepts the two built-in URLs (https://ip.164746.xyz/ipTop.html and https://ip.v2too.top/api/nodes) or the TOML base URL; changing it forces preview unless confirm_apply=true. Omitted source_url preserves the saved selection across restarts. dry_run false requires explicit confirm_apply true. No secret ever returned. Empty new token input means preserve (omit from secrets), null means explicit clear. Reject removing credentials used by configured targets. Validate while no task running; conflict 409 instead of waiting on network. Use durable private file under state directory; no automatic DNS run merely for save, next scheduled task uses saved config. Changing schedule wakes sleeping loop/recomputes next run.
- POST /api/admin/source/refresh {} → 202 {accepted:true}; authenticated Origin+CSRF protected source-only refresh. Reserves the shared task lock before responding (busy: 409), retains DNS deadlines and pending operations, and never constructs DNS providers or sends notifications. Poll source_status/source_error for completion; failure retains display IPs but revokes their DNS eligibility.
- POST /api/admin/run {dry_run:bool,confirm_apply?:bool} → 202 {accepted:true}; atomically reserve task before response, refuse duplicates with409. Preview overrides apply without changing saved config; apply only if saved config.dry_run false and confirm_apply true. Status polled via GET.

On first admin-enabled serve generate random administrator password, persist hash and a private initial-password file under /data (not logs). Backend CLI may accept bootstrap password through explicit test-only env for isolated tests. Initial username admin. Frontend never contains credential defaults.

## Boundaries
No actual provider credentials on the local deployed preview. No remote DNS writes, no random-image-api mutations. Server hosts Vite dist from web/dist in image, secure static allowlist plus SPA route fallback. Generated secrets/config/state excluded from build/archive. Local published port remains 127.0.0.1:8788.
