# 映库网页

Vue 3 + Vite + ArtPlayer + hls.js 的私人媒体前端。源码在 `src/`，与 Emby 兼容服务共享 `catalog.json`，通过 AList 获取带签名的 HLS 入口。

支持登录、搜索标题/编号/演员、分类、收藏、继续观看、影片详情、拖动和移动端。收藏与进度保存在本机浏览器，不与桥接 SQLite 同步。登录使用 AList 账号，密码不持久化；标签页会话保存在 sessionStorage，生产环境建立 HttpOnly / Secure / SameSite Cookie。

```sh
npm ci
# 先从自己的媒体目录生成 public/catalog.json，或从 NAS 同步：
python3 scripts/build_catalog.py --root /srv/media-hls --output public
# 或：python3 scripts/sync_catalog.py --host YOUR_SSH_HOST
ALIST_ORIGIN=http://127.0.0.1:5244 npm run dev
```

开发地址 `http://127.0.0.1:5178/cinema/`。`ALIST_ORIGIN` 配置开发 API / 下载代理；`VITE_ALIST_HOME` 可配置“打开 AList”链接。正式站与 AList 使用同源 `/api/`、`/d/`、`/p/`。

```sh
node --test tests/*.test.mjs
CINEMA_PUBLIC_DIR=../examples/cinema npm run build
npx playwright install chromium
npm run test:browser
```

`CINEMA_PUBLIC_DIR` 指定构建静态数据目录，合成例子不会提供真实播放。部署时通常用默认 `public/`；构建产物 `dist/` 含自己的片库时也属于私人数据。Python 测试请使用仓库根目录安装的虚拟环境。

正式部署必须同时配置 `nginx-pan.conf` 与 `nginx-cinema-auth.lua`。需要 OpenResty 或 Nginx Lua 模块；入口 HTML 和 JS/CSS 公开，片库、封面等静态数据每次都经 AList `/api/me` 验证。普通静态托管不会提供这一层保护。完整步骤见 [部署文档](../docs/deployment.md)。

远程维护工具：

```sh
# 使用仓库根目录的虚拟环境：
../.venv/bin/python scripts/import_remote_metadata.py TEST_001 --host YOUR_SSH_HOST --apply
../.venv/bin/python scripts/remote_cover.py --host YOUR_SSH_HOST --list
../.venv/bin/python scripts/remote_cover.py TEST_001 --host YOUR_SSH_HOST --preview --time 00:00:30
../.venv/bin/python scripts/remote_cover.py --host YOUR_SSH_HOST --apply-preview PREVIEW_ID
```

服务器需安装 `cinema-import` 包装入口并配置环境文件。预览状态路径可用 `--remote-state` 或 `SCRAPER_STATE_PATH` 覆盖。本机预览保存在忽略的 `outputs/`。

`npm run test:browser` 自动运行合成登录与浏览测试。`verify_server_auth.py` 在自己的 HTTPS 部署上验证匿名访问、无效凭据、Cookie、封面和播放文件入口；`verify_browser.mjs` 可进一步验证实际播放、拖动、跨 pack 和续播。配置方式见部署文档，真实部署检查不在普通测试中执行。
