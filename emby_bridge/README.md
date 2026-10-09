# Emby 协议兼容服务

实现位于 `server.py`，根目录 `server.py` 为兼容启动入口。Python 标准库服务读取Web 片库索引，提供 Emby 客户端需要的识别、登录、片库、封面、HLS、进度、收藏和会话接口。

字幕从 `MEDIA_ROOT` 下的清单同目录发现，支持 UTF-8 SRT / WebVTT。`PlaybackInfo` 与影片详情返回外挂字幕轨道、默认字幕索引和标准字幕接口。接口支持 VTT / SRT、HEAD、起始时间参数，并验证会话与影片访问权限；无需烧录字幕或重新转码视频。目录及命名约定见 [外挂字幕说明](../docs/media-pipeline.md#外挂字幕)。

以 AList 管理员账号登录。普通模式向 AList 验证账号；同机模式只读 AList SQLite / config，使用桥接自身 30 天会话并检查账号撤销。观看记录保存在 `STATE_PATH` 的 SQLite 中。

```sh
CATALOG_PATH=examples/catalog.json STATE_PATH=./state python3 server.py
```

在仓库根目录执行以上命令；实际部署配置 `PUBLIC_ORIGIN`、`ALIST_ORIGIN`、`CATALOG_PATH`、`STATE_PATH`，可选同时配置 `ALIST_DATABASE` 与 `ALIST_CONFIG`。systemd 与 Nginx 模板在 `deploy/`。

兼容服务不提供在线播放转码、完整管理后台、插件系统或普通用户目录权限映射；它依赖已准备好的媒体和可访问的 HLS 内部签名。网页和 Emby 客户端的收藏/进度独立。

`tests/` 使用临时数据库和合成片库测试。`verify_live.py` 由操作员在自己的服务器显式执行，建立并撤销临时会话，检查片库、图片、HLS、FFmpeg 播放与跨包解码；默认不改观看进度和媒体缓存。`--write-media` 保存探测结果；`--check-progress` 临时写入并恢复一条进度，应在该账号空闲时运行。`verify_sessions.py` 复制现有会话验证同机迁移，并在完成后删除副本，不修改观看进度。

具体部署与验收命令见 [部署文档](../docs/deployment.md)。

在加载运行环境的 AList 主机执行 `CINEMA_TEST_VIDEO_ID=影片目录 .venv/bin/python emby_bridge/verify_subtitles.py`，可以验证真实字幕轨道、SRT / VTT、HEAD、匿名拒绝和网页签名回读。脚本建立临时测试会话，并在结束时撤销；不会修改观看进度。
