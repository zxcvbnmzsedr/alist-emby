# 完整源码范围

仓库以原工程为代码基础，保留 `cinema/`、`emby_bridge/`、`scripts/` 和测试目录。发布范围包括以下原有功能：

| 模块 | 已纳入内容 |
|---|---|
| 映库 | Vue 页面、样式、片库工具、播放器、登录和会话处理、Vite 配置、npm 锁文件 |
| Emby 兼容 | 服务全部实现、账号模式、会话迁移、图片授权、播放与观看记录、测试和验收脚本 |
| 索引 | NFO 读取、实际 HLS 片长、封面生成、SSH 目录同步 |
| 刮削 | 元数据核对和导入、图片校验、自动与手动截帧、远程查询/预览/发布、provider 源码 |
| 上传指导与 skill | 原上传流程说明的通用公开版本、`.agents/skills/alist-media-upload` 入口及所需引用文件 |
| 入库流水线 | 本机打包与加密、断点 SSH 传输、AList 任务队列、云端 Range/解密解码校验、清单与索引发布、状态查询 |
| 清理 | 经过发布核验和密钥备份核对的原片清理、任务文件清理、显式删除开关 |
| 配套 | HTTPS / Lua 鉴权 / systemd 模板、环境示例、完整流程文档、Python / 浏览器测试和 CI |

补充了通用视频清单扫描入口，原先写死的 SSH 主机、路径和域名改为参数或环境变量。macOS 默认 VideoToolbox，Linux 默认 libx264，可显式选择编码器。历史 CLI 入口保留兼容包装。

不纳入的是部署者的私有数据和生成物：真实媒体、AES key、Token、签名清单、原始私人 NFO 和图片、实际 catalog、工作任务和运行状态、数据库、日志、截图报告、node_modules、虚拟环境和构建产物。`.env.example`、`deploy/*.example` 和 `examples/` 提供可填写配置与合成资料。

AList 程序、云盘账号以及第三方 Emby 客户端属于外部依赖；仓库提供接入逻辑。随仓上游 provider 的源码保持在 `cinema/vendor/jav-metadata-syncer/`，其使用方式与来源见该目录说明。
