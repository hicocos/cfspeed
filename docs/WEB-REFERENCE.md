# Web 参考与边界

参考源码：`reference-project/web`。参考项目未修改；未登录真实参考账户，也未写入其 API。

## 直接复用
原样复制 `style.css`、`design.css`、`compact-admin.css`、`mkw-public.css`、`mkw-admin.css`，以及 Icon/Modal/Notice/EmptyState/CodeBlock、usePageMotion 和两张 SVG 纹理。`reference-files.json` 记录逐文件 SHA256。

AdminLayout 与 Login 模板保留原侧栏、顶栏、登录框、按钮、字体、边框、圆角、层叠背景及手机断点。Overview 的统计区保留原两列四卡布局。导航和业务内容替换为 cfspeed。`cfspeed.css` 只补充业务内容样式。

这不是把随机图功能搬进来：无图片库、存储管理、随机内容统计；所有运行数据来自 cfspeed。

## 来源与验证边界

前端样式和组件来自 random-image-api 项目；`reference-files.json` 保留复用文件的来源校验记录。生产参考站浏览器脚本和本机验收报告不包含在此公开快照中。`tests/frontend_fixture.py` 使用明确的模拟数据，不代表真实 DNS 写入验证。
