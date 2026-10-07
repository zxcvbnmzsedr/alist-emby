# 加密视频切片上传流程

使用 alist-emby 的命令行工具将视频生成独立加密短段、合并为存储包，通过 AList 上传并验证，再发布播放清单和媒体索引。

## 原理与默认值

约 6 秒 HLS 播放段各自 AES-128-CBC 加密，再顺序拼成约 20 分钟、最多约 512 MiB 的 pack。播放器按清单中的 `EXT-X-BYTERANGE` 发出小范围请求，不必下载整个 pack。必须先逐段加密再合并；不能把整段 20 分钟加密一次后只修改清单。

| 项目 | 默认做法 |
|---|---|
| 原片 | 操作员明确指定的源目录；默认保留 |
| 本机私有任务 | 工程 `work/import/`，或 `ALIST_EMBY_WORK_DIR` |
| NAS 临时 pack | `BATCH_PACKS_PATH=/srv/alist-emby/packs` |
| 对应 AList 本地 pack 根 | `ALIST_LOCAL_PACK_ROOT=/local/packs` |
| AList 云盘 pack 根 | `ALIST_SEGMENT_ROOT=/cloud/raw` |
| NAS 正式媒体目录 | `MEDIA_ROOT=/m3u8`，AList 挂载为 `/m3u8` |
| key / 清单 | `/m3u8/影片目录/key` 与 `index.m3u8`，签名访问 |
| 索引 / 封面 | `CATALOG_OUTPUT=/srv/alist-emby/www/cinema`，网页鉴权 |
| 短段与 pack | 目标 6 秒、约 20 分钟 / 最大 512 MiB；EXTINF 取实际时长 |
| 编码 | 兼容 H.264 8bit 直接封装，AAC 保留；其余按配置转码 |
| 加密 | 每影片随机 16 字节 key，每段随机 16 字节 IV，独立 padding |

源视频在 NAS 时，先明确打包执行位置。可以在 NAS 上运行工具并以同机 SSH 目标调度服务端，或经授权取得工作站副本后走本机流程。远端绝对路径不属于本机文件，扫描器不会代为下载。

云盘由 AList 挂载提供。同一挂载可负责上传与播放代理；上传路径必须可写，播放路径必须可读且支持 Range。

## 操作顺序

1. 核对实际源文件、下载完成状态、大小、mtime、真实时长、编码和可用空间。尊重用户指定的质量 / 不转码要求；`-hls_time 6` 不保证每段精确 6 秒，实际分段取决于关键帧。
2. 检查 AList 本地 pack 路径准确映射 NAS 文件系统目录，云盘目标可上传，媒体挂载启用签名。凭据留在配置该 AList 的受限主机；不写进公开脚本。
3. 生成完整 VOD HLS 清单，独立加密每段后合并。保存每段真实时长、pack、密文长度、offset、IV；pack 保存大小和 SHA-256。
4. 本机通过全清单解密读取检查封装和总时长，保留 key / 清单 / 结构记录。此检查不等于逐帧完整解码。
5. SSH 比较远端已存在 pack 的大小与 SHA-256，只传缺包；私有元数据传到 NAS 状态目录。
6. AList 顺序复制 pack 到云盘，等待任务实际完成，检查云端文件大小；不能只看进度达到 100%。
7. 从每个 pack 首末段请求精确字节范围，要求 206 和正确 Content-Range，比较密文字节；以对应 key / IV 解密并执行 FFmpeg 解码。
8. 签名 key 与 pack 路径，清单使用同源 `/d/m3u8/...` 和 `/p/云盘挂载/...` 资源 URI。不写固定内网 IP、协议或个人域名。
9. 在私有媒体挂载发布 key 与清单，回读正式签名清单，确认不带签名的 key / 清单访问被拒绝，生成 catalog。已有目标须属于同一任务，拒绝覆盖无关影片。
10. 检查真实首播、拖动、跨 pack 与续播，最后交付播放入口和核验范围。默认保留原片及密钥备份；生成 pack 可在成功核验后清理。

自动工具已实现上述打包、传输、上传与服务端发布顺序。浏览器与客户端验收需要部署者在实际网络中执行，不能从本地单元测试推断播放性能或多用户承载能力。

## 命令入口

在已经配置的 alist-emby 仓库根目录执行：

```sh
# 新批次：扫描自己的目录，不覆盖既有清单。
.venv/bin/python scripts/inventory.py /path/to/your/videos

# 运行；--limit 可限制本次处理量。
.venv/bin/python scripts/batch_media.py --host YOUR_SSH_HOST --limit 1
.venv/bin/python scripts/batch_status.py

# 修复失败原因后恢复同一任务。
.venv/bin/python scripts/batch_media.py --host YOUR_SSH_HOST --retry-failed
```

恢复任务时，将 `ALIST_EMBY_WORK_DIR` 指向保存清单、状态和 key 的工作目录，不要重建 inventory。STOP 文件用于影片之间暂停，移除后可以继续。源目录变化时核对现有任务，只有明确开始新扫描才用 `--replace`。

NAS `scripts/cloud-command` 与 `cinema-import` 包装入口读取自己的 `/etc/alist-emby.env`。远程工程目录由 `ALIST_EMBY_REMOTE_ROOT` 指定；本机与服务端的 `BATCH_*` 路径必须一致。部署细节在仓库 `docs/deployment.md`，完整运行与清理在 `docs/media-pipeline.md`。

签名到期或挂载迁移后可在 NAS 执行：

```sh
/opt/alist-emby/scripts/cloud-command publish JOB_ID
```

保持原 key / IV / BYTERANGE，仅重新发布当前资源签名；修改前备份正式清单，并在发布后核验。桥接服务只刷新入口签名，不更新清单内的 key / pack 签名。

## 清单结构

下面使用占位符，必须以真实密文索引替换；各 URI 中的签名也是占位符：

```m3u8
#EXTM3U
#EXT-X-VERSION:4
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-KEY:METHOD=AES-128,URI="/d/m3u8/example/key?sign=KEY_SIGNATURE",IV=0x00000000000000000000000000000001
#EXTINF:6.000000,
#EXT-X-BYTERANGE:1048576@0
/p/cloud/raw/example/pack_000.ts?sign=PACK_SIGNATURE
#EXT-X-KEY:METHOD=AES-128,URI="/d/m3u8/example/key?sign=KEY_SIGNATURE",IV=0x00000000000000000000000000000002
#EXTINF:6.000000,
#EXT-X-BYTERANGE:1048576@1048576
/p/cloud/raw/example/pack_000.ts?sign=PACK_SIGNATURE
#EXT-X-ENDLIST
```

BYTERANGE 的长度和 offset 都基于加密后的字节，不能用明文长度；padding 会改变大小。换 pack 后 offset 相对于新的文件计算。TARGETDURATION 至少为最大实际段长向上取整，示例值不代替真实计算。

## 验收与常见失败

- 浏览器需实际发出 `Range: bytes=起点-终点`，收到精确 206 / Content-Range 并可解密；服务端声称支持 Range 不代表播放器用了小范围读取。
- `cinema/scripts/verify_browser.mjs` 读取受限环境中的 CINEMA_URL、CINEMA_TEST_VIDEO_ID、CINEMA_TOKEN；CINEMA_TEST_BOUNDARY_SECONDS 指定已知跨 pack 时间。不提供边界时只证明普通拖动。
- 浏览器脚本检查首播、拖动、续播、206 和页面错误；Emby 客户端仍需单独验收。首次测试结果不应成为速度或并发承诺。
- 上传失败先看任务真实错误、云盘登录状态及空间；失败任务保持原 key 和已完成 pack，避免重复打包。
- 上传后读不到新目录时刷新云盘父目录与目标目录缓存，核对实际读写挂载路径。
- 反向代理必须保留 Range、206 和 Content-Range，正确转发 `/d/`、`/p/`。AList `/p/` 代理仍可能占服务器带宽。
- 能拿到有效签名清单和 key 的客户端可以保存解密内容；这是媒体访问方案，不是 DRM。签名的真实期限取决于当前 AList，勿宣称固定时限。
- key 丢失无法恢复正常播放；清理时将密文、对应清单与 key 作为一组核对，保留正在使用的 key 与备份。

## 给代理的请求示例

> 请使用 alist-media-upload skill，将我指定主机上的源目录按独立短段 AES-128 + BYTERANGE 方案入库。复用已有 AList 挂载和 SSH 配置，保留兼容编码与原片，执行云端范围读取和解码检查，验证播放后报告任务与入口。不要把密钥、Token 或私人片库写进公开仓库。
