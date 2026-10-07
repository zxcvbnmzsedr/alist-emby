# 完整部署

方案包含 AList、Web 片库静态网页、Emby 兼容服务和入库 / 刮削 CLI。推荐 HTTPS 同源：`/cinema/` 为网页，`/emby/` 与标准根 API 为兼容服务；AList 保留 `/api/`、`/d/`、`/p/`。

## AList 与路径

配置自己的云盘、本地挂载和签名访问，确保授权账号能读媒体，未签名 key 和清单被拒绝。完整流水线默认：

| 文件系统目录 | AList 逻辑路径 |
|---|---|
| NAS `/m3u8` | `/m3u8` |
| NAS `/srv/alist-emby/packs` | `/local/packs` |
| 你配置的云盘挂载 | `/cloud/raw` |

每影片一个目录，包含 `index.m3u8`、可选 NFO / poster / fanart。媒体和 key 保持私有，索引输出到独立目录。已准备好的 HLS 可直接生成索引，不必重新入库。容器部署要区分宿主与容器路径，见 [媒体流程](media-pipeline.md)。

## 服务器安装

服务器需要 Python 3.11+、FFmpeg、ffprobe、OpenSSL。将仓库放在 `/opt/alist-emby`：

```sh
cd /opt/alist-emby
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-scraper.txt
sudo useradd --system --home /var/lib/alist-emby --shell /usr/sbin/nologin alist-emby
sudo install -d -m 0700 -o alist-emby -g alist-emby /var/lib/alist-emby
sudo install -d /srv/alist-emby/www/cinema /srv/alist-emby/packs /m3u8
sudo install -m 0600 .env.example /etc/alist-emby.env
sudo install -m 0755 cinema/scripts/cinema-import /usr/local/bin/cinema-import
```

编辑环境文件；服务器 CLI 需 `ALIST_TOKEN` 或只读 `ALIST_DATABASE`。CLI 运行账号须能读配置和媒体/key、写任务和索引；桥接服务只需读索引和写自己的 SQLite。按实际服务用户授予目录读取/遍历权限。

`cinema-import` 与 `scripts/cloud-command` 自动读 `/etc/alist-emby.env`，可用 `ALIST_EMBY_ENV_FILE` 覆盖；其他 CLI 不自动加载，先用 `set -a; . /etc/alist-emby.env; set +a` 加载受限配置。路径变化时同步调整环境、unit 与网页配置。

## 前端与索引

Node.js 22.12+；可在本机构建后传到 NAS。初次部署先复制应用壳，再生成真实片库：

```sh
cd /opt/alist-emby/cinema
npm ci
CINEMA_PUBLIC_DIR=../examples/cinema npm run build
sudo cp -R dist/. /srv/alist-emby/www/cinema/
cd /opt/alist-emby
.venv/bin/python cinema/scripts/build_catalog.py --root /m3u8 \
  --output /srv/alist-emby/www/cinema
```

后续前端更新复制 `index.html` 与 `assets/`，保留现有 catalog / covers。入库和刮削自动更新索引，手工修改 NFO / 图片后再运行生成器。

## Emby 服务与账号

```sh
sudo install -m 0644 deploy/alist-emby.service /etc/systemd/system/alist-emby.service
sudo systemctl daemon-reload
sudo systemctl enable --now alist-emby
curl http://127.0.0.1:8097/emby/System/Info/Public
```

客户端选择 Emby，地址与 `PUBLIC_ORIGIN` 的协议、域名、外部端口一致，以 AList 管理员账号登录。普通模式向 AList 验证；同机模式同时配置 `ALIST_DATABASE` 和 `ALIST_CONFIG`，检查账号撤销并使用自身 30 天会话。

unit 默认 `ProtectHome=true`。AList 数据在 `/root` 时应使用可读路径或针对性调整隔离；SQLite WAL / SHM 和父目录也要有必要权限。启用后检查登录、重启与撤销。

## HTTPS 和网页鉴权

完整网页需 OpenResty，或加载 Lua 模块和 `cjson.safe` 的 Nginx。用 `cinema/nginx-pan.conf` 作为独立 server 模板，替换域名、TLS 证书、网页根目录与 upstream；把 `cinema/nginx-cinema-auth.lua` 安装到模板指定路径，先 `nginx -t` 再 reload。

应用壳与 assets 公开；catalog、covers 及其余私有静态资源每次通过 AList `/api/me` 验证。`/cinema/session` 设置 HttpOnly / Secure / SameSite Cookie，`/cinema/api/fs/get` 支持 Cookie 转发。缺少 Lua 配套会失去静态数据保护。当前 AList guest role ID 按 1 拒绝，角色模型改变时需复核。

仅用 Emby 时可用普通 Nginx 的 `deploy/nginx-location.conf.example`。容器中的 loopback 不等于宿主，配置真实可达 upstream 和桥接监听地址。标准根 API 与同站点已有应用需检查路由冲突，保留 AList 下载路由。

## 维护与验收

[入库流程](media-pipeline.md) 和 [刮削说明](scraper.md) 提供完整 CLI。在服务器加载自己的环境后：

```sh
cinema-import TEST_001 --apply
cinema-import --auto-cover
.venv/bin/python cinema/scripts/verify_server_auth.py
.venv/bin/python emby_bridge/verify_live.py
# 同机模式，有现有会话时：
.venv/bin/python emby_bridge/verify_sessions.py
```

检查匿名和无效凭据被拒绝，再验证实际账号的片库、图片、播放、拖动和跨 pack；接口 200 本身不能证明可播放。兼容检查默认不改观看记录；`--check-progress` 临时写入并恢复一条记录，运行时账号应空闲；`--write-media` 显式保存探测结果。

浏览器实播工具为 `cinema/scripts/verify_browser.mjs`。在受限本机环境设置 `CINEMA_URL`、`CINEMA_TEST_VIDEO_ID`、`CINEMA_TOKEN`，然后在 cinema 执行 `node scripts/verify_browser.mjs`。可用 `CINEMA_TEST_BOUNDARY_SECONDS` 指定已知跨 pack 时间，否则检查普通拖动；`BROWSER_EXECUTABLE` 可复用现有 Chromium。实际 Token 不写命令历史或源码。合成 UI 验证用 `npm run test:browser`，不连接 NAS。

## 更新与恢复

保留旧代码和配置；SQLite 用在线备份或停服务备份，勿仅复制运行中的 WAL 主文件。回滚代码前确认会话结构兼容，不用旧状态覆盖后续进度。中断入库复用任务和 key；签名失效重新发布，不生成新 key 覆盖既有密文。
