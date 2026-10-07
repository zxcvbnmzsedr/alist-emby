---
name: alist-media-upload
description: 使用 alist-emby 将指定视频切成独立加密的 HLS 短段、合并 pack 并通过 AList 上传云盘，验证 Range、解密和播放后发布。适用于媒体入库、上传恢复和签名清单重新发布。
---

# AList 媒体上传

使用 alist-emby 仓库已有工具完成用户指定的视频入库。先读 [上传指导](references/upload-workflow.md)，再定位仓库：优先使用包含上述工具的仓库目录，安装为全局 skill 时可由 `ALIST_EMBY_REPO` 指定克隆目录。仓库应包含 `scripts/batch_media.py`、`scripts/batch_cloud.py` 和 `.env.example`。

## 开始前确定

- 确认源文件所在主机、实际路径、SSH 目标，以及 AList 的本地 pack 挂载、云盘根目录和媒体挂载。不要把远端文件路径直接交给本机扫描器。
- 读取现有任务清单和状态；恢复时复用任务、密钥与已验证 pack。检查实际源文件身份、编码和本机 / NAS 空间。
- 配置只从操作员受限环境读取，示例不包含可用凭据。沿用已有 AList 与 SSH 登录方式。
- 按用户已授权范围完成打包、上传、校验和发布。用户要求保留编码或质量时先核对编码策略；默认保留原片，删除需要明确授权。

## 执行与证据

1. 新批次使用 `scripts/inventory.py` 扫描明确指定的源目录；已有批次直接恢复，避免重建清单丢失任务关联。
2. 用 `scripts/batch_media.py --host SSH_HOST` 调度。它生成短 HLS 段，逐段 AES 加密后合并 pack，通过 SSH 传输，并调用服务端 `scripts/cloud-command` 进行 AList 上传、核验和发布。
3. 首次失败停止继续添加任务；读取对应任务日志，修复原因后使用 `--retry-failed`。不要用新 key 覆盖既有云端密文。
4. 发布成功仍需真实播放检查。明确区分本机全清单解密读取、云端首末段解码、浏览器首播 / 拖动 / 跨 pack 和客户端验收，未执行的检查不要报告为通过。
5. 反馈影片 ID、任务结果与未带签名的逻辑清单路径；私有报告继续留在工作目录。签名能力链接仅在用户需要时通过其受限交付渠道提供，不写入公开文档。

## 不可破坏的格式约束

每影片使用随机 16 字节 key，每个短段独立 IV、独立 AES-128-CBC 和 PKCS#7 padding。必须先加密短段，再合并；BYTERANGE 按密文长度 / 偏移计算。EXTINF 使用实际时长，TARGETDURATION 根据最长段向上取整，结尾含 ENDLIST。

云端只上传密文；key 与正式清单留在签名访问的私有媒体挂载。验证精确 206 / Content-Range、云端密文一致和解密解码，不能用上传百分比或清单 HTTP 200 代替。播放链接用同源资源路径，保留 AES / IV / BYTERANGE。

无需复制仓库代码进 skill；安装本 skill 后仍需 alist-emby 工程和运行依赖。详细命令、清单例子、恢复与验收见引用的上传指导。
