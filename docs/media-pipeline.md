# 视频扫描、加密打包与云端入库

完整链路包含本机 `inventory.py` / `batch_media.py` 与 NAS `batch_cloud.py`。通过 AList 已配置的云盘挂载上传，key 和清单保留在 NAS，云盘只保存加密 pack。

## 路径配置

| 数据 | 默认位置 / 变量 |
|---|---|
| 本机任务、进度与密钥备份 | 工程 `work/import/` / `ALIST_EMBY_WORK_DIR` |
| NAS 任务与密钥备份 | `/var/lib/alist-emby/batches` / `BATCH_STATE_PATH` |
| NAS 临时 pack | `/srv/alist-emby/packs` / `BATCH_PACKS_PATH` |
| 对应 AList 本地挂载 | `/local/packs` / `ALIST_LOCAL_PACK_ROOT` |
| AList 云盘 pack 根目录 | `/cloud/raw` / `ALIST_SEGMENT_ROOT` |
| NAS 媒体根目录 | `/m3u8` / `MEDIA_ROOT`；AList 逻辑路径须为 `/m3u8` |
| 私有片库、封面与网页目录 | `/srv/alist-emby/www/cinema` / `CATALOG_OUTPUT` |

本机 `BATCH_*` 路径参数用于 SSH / SCP 目标，必须与 NAS 配置一致。AList 本地挂载应准确映射 pack 文件系统目录；云盘挂载须可读写且支持 Range。容器中的实际路径也须匹配。NAS 安装与包装入口见 [部署文档](deployment.md)。

## 扫描、运行与恢复

本机和 NAS 均需 FFmpeg、ffprobe、OpenSSL；SSH 已配置无交互登录。在本机仓库根目录执行：

```sh
.venv/bin/python scripts/inventory.py /path/to/your/videos
.venv/bin/python scripts/batch_media.py --host YOUR_SSH_HOST --limit 1
.venv/bin/python scripts/batch_status.py
# 首次试跑成功后去掉 --limit；失败任务需要显式重试。
.venv/bin/python scripts/batch_media.py --host YOUR_SSH_HOST --retry-failed
```

也可设置 `ALIST_EMBY_SSH_HOST` 和 `ALIST_EMBY_REMOTE_ROOT`。源路径、大小与 mtime 确定任务 ID；同编号文件获得不同目标目录。默认不覆盖清单，重新扫描时需显式 `inventory.py --replace ...`，旧任务与 key 备份继续保留。媒体符号链接会被拒绝。

H.264 8bit 默认只封装，兼容 AAC 音轨保留，其他音轨转 AAC，其他视频转 H.264。macOS 默认 VideoToolbox，Linux 默认 libx264，可用 `ALIST_EMBY_VIDEO_ENCODER` 覆盖。`--preserve-hevc` 用于已确认支持 HEVC 的客户端。

## 执行顺序

1. 核对空间、原片身份、既有任务、NAS 与云盘同名目录。
2. FFmpeg 生成约 6 秒 HLS 分片，每影片随机 16 字节 key，每段随机 16 字节 IV；AES-128-CBC 加密后合并 pack，单包约束 512 MiB / 1200 秒。
3. 保存真实 EXTINF、独立 IV、加密后的 BYTERANGE、ENDLIST、pack 哈希及结构记录。本机 FFmpeg 解密并读取全清单检查封装，不等价于逐帧完整解码。
4. SSH 核对已有包的大小和 SHA-256，SCP 只传缺包及私有元数据。
5. AList 一次提交一个云端复制任务，轮询与恢复上传。
6. 每个 pack 首末段执行云端 Range 请求，要求精确 206 / Content-Range；比较密文，AES 解密后由 FFmpeg 实际解码。
7. 签名 key 与 pack 链接，发布清单与索引；读回签名清单，并确认未签名 key / 清单被拒绝。
8. 尝试刮削；在线封面不足时检查第 3 秒截帧。可选资料失败不阻塞已验证播放。
9. 清理经核验的生成 pack，保留 key、清单与任务记录；默认保留原片。

创建 `work/import/STOP` 在影片之间停止；删除后重跑继续。文件锁阻止多个队列并行，遇首个未解决失败停止当前队列。中断后保留状态、日志与 key，不要重新生成 key 覆盖已上传的密文。

## 显式原片清理

```sh
# 入库后逐部重新核查云端和本机 key 备份，再删除未变化的原片。
.venv/bin/python scripts/batch_media.py --host YOUR_SSH_HOST --delete-completed-sources

# 已完成任务：刷新云端审核，生成清理计划，默认不删除。
.venv/bin/python scripts/cleanup_completed.py --source-root /path/to/your/videos \
  --refresh-audit --host YOUR_SSH_HOST

# 检查计划后，显式执行：
.venv/bin/python scripts/cleanup_completed.py --source-root /path/to/your/videos \
  --refresh-audit --host YOUR_SSH_HOST --apply
```

独立清理器要求 15 分钟内审核记录，先验证整个计划再删除，拒绝越界路径、符号链接、变化原片和不匹配 key。删除不能撤回，key、清单与记录继续保留并由你备份。

## 签名与播放

AES 保护云盘字节，授权客户端取得 key 后可以解密，不属于 DRM。桥接服务只刷新入口清单签名，对内部链接执行 `urljoin`，不会刷新内部签名。签名过期或挂载变化时，可在服务端执行 `scripts/cloud-command publish JOB_ID`，保留原 key / IV / BYTERANGE，重新发布并验证真实播放。云盘品牌不限，必须验证其实际挂载上传、代理与 Range 能力。
