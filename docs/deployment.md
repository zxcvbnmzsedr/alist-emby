# 部署、升级与回滚

## 前置条件

- Python 3.11+；服务和索引生成器都只有标准库依赖。
- 已运行并具有可用管理员账号的 AList。
- AList 能读取 `/m3u8/<id>/index.m3u8`，清单中的密钥和分片链接可从客户端访问。
- 公共 HTTPS origin 同时提供 AList 和兼容接口，反向代理保留 Range/206。

推荐先使用普通 API 模式部署，再根据需要启用同机账号模式。项目没有自动安装或修改 AList。

## Linux systemd 示例

以下命令在已 clone 的仓库目录执行，安装到约定路径。根据现有系统修改用户、路径和 Python 可执行文件：

```sh
sudo useradd --system --home /var/lib/alist-emby --shell /usr/sbin/nologin alist-emby
sudo install -d -m 0755 /opt/alist-emby /srv/alist-emby/catalog
sudo install -m 0644 server.py /opt/alist-emby/server.py
sudo install -m 0600 deploy/alist-emby.env.example /etc/alist-emby.env
sudo install -m 0644 deploy/alist-emby.service /etc/systemd/system/alist-emby.service
```

修改 `/etc/alist-emby.env` 中的地址和路径，生成片库索引：

```sh
sudo python3 scripts/build_catalog.py \
  --root /srv/media-hls --output /srv/alist-emby/catalog
sudo systemctl daemon-reload
sudo systemctl enable --now alist-emby
sudo systemctl status alist-emby
```

索引及封面目录须对服务用户可读、各级目录可遍历；模板由 systemd 创建私有状态目录。媒体目录必须已作为 AList `/m3u8` 挂载。不要给兼容服务运行用户不需要的写权限。

将 `deploy/nginx-location.conf.example` 插入现有 AList 的 HTTPS server 块，运行 `nginx -t`，通过后 reload。若 Nginx 在 Docker 内，`127.0.0.1` 是容器自身，应配置它实际可达的宿主或服务地址，并相应调整桥接监听地址。模板中的 `Movies`、`LiveTv` 等根路由需要与同站点已有应用检查冲突。

`PUBLIC_ORIGIN` 与浏览器/客户端使用的协议、域名、外部端口保持一致。如果原有 `location /` 代理 AList，继续保留；不能把 `/d/`、`/p/` 或 `/api/` 交给兼容服务。

## 同机账号模式

在配置中同时设置 `ALIST_DATABASE`、`ALIST_CONFIG`，授予服务用户对指定文件及父目录必要的读取/遍历权限，再重启兼容服务。不要复制管理 Token 到客户端配置。

模板使用非 root 用户、只读系统目录和 `ProtectHome=true`。AList 数据位于 `/root` 时该模板不能直接读取，应该选择服务可读的部署路径或有针对性地调整隔离配置。SQLite 使用 WAL 时还需确保只读连接能读取相关 WAL/SHM 文件；启用后实际验证登录、重启及账号撤销，不能仅以进程启动成功为准。

## 验收

```sh
python3 -m unittest discover -s tests -v
curl https://media.example.com/emby/System/Info/Public
sudo journalctl -u alist-emby -n 50 --no-pager
```

公共识别接口可匿名访问。匿名或错误 Token 的 `/emby/Items?Recursive=true` 应返回 401。用真实客户端验证管理员登录、封面、播放、拖动、续播和退出；不能只以接口 200 判断可播放。

典型问题：

| 现象 | 检查 |
|---|---|
| 服务没有收到客户端请求 | 地址、端口、`/emby` 基础路径及反向代理路由 |
| 登录成功后片库为空 | 索引路径、权限、`videos` 与 `/m3u8` 路径约定 |
| 片库有内容、封面失败 | 本地 covers 路径、文件权限、客户端图片签名传递 |
| 清单返回 200，但播放失败 | 内部 URI、签名期限、Range、AES IV/密钥和媒体编码 |
| 几天后普通模式返回 401 | 上游 AList 登录 Token 是否到期；重新登录或评估同机模式 |

## 更新与回滚

更新前运行测试，使用 SQLite 在线备份接口或停服务后备份整个状态目录；运行中的 WAL 数据库不要只复制主文件。保留上一份代码、配置和当前状态备份，替换代码后重启服务，验证片库与播放。

只回滚代码时应先确认数据库结构兼容，尤其是同机模式的会话列与已迁移记录。不要用旧状态库覆盖后续观看进度。彻底停止服务时移除自己添加的兼容 API 路由，检查 Nginx 语法，再停用 systemd 单元；继续保留 AList 的原路由及媒体目录。
