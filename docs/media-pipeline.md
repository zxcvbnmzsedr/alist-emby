# 加密 HLS 与云端存储

本页说明完整媒体方案。仓库提供片库索引生成和播放适配服务；上传调度、切片打包和签名清单发布由部署者接入自己的流程。

## 数据布局

| 数据 | 建议位置 | 用途 |
|---|---|---|
| 加密 TS 分片或 pack | AList 挂载的云端目录，如 `/cloud/raw/<id>/` | 大体积媒体存储 |
| `index.m3u8` | NAS 私有目录，AList 路径 `/m3u8/<id>/` | 片段顺序、时长、AES 参数和字节范围 |
| `key` | 与清单同目录，保留离线私有备份 | 每个影片的 16 字节 AES-128 密钥 |
| NFO、poster、fanart | NAS 影片目录 | 描述与图像 |
| catalog、covers | 独立索引目录 | 兼容服务读取，不开放匿名静态路由 |

不将密钥上传到存储加密分片的云盘，可避免该存储单独取得解密材料；通过播放账号取得密钥的客户端仍能解密媒体，这不属于 DRM。

## 准备与加密

1. 用 `ffprobe` 取得真实片长、编码和轨道信息。将媒体准备为目标客户端可直接播放的 H.264/AAC 等组合；兼容服务没有在线转码能力。
2. 以 FFmpeg 生成 VOD HLS TS 分片。兼容编码可直接封装，否则离线转码；直接封装时不能仅靠设置 HLS 标记保证每段独立可解码。
3. 每个影片生成独立 16 字节随机密钥，每段选取独立的 16 字节 IV。用 AES-128-CBC 加密，并使用 PKCS#7 padding；保留用于恢复的 IV、密钥和包结构记录。
4. 可以逐段保存，也可以把每个**独立加密后的段**串联为较大的 pack，降低云端小文件数量。清单的 `BYTERANGE` 长度和偏移必须指向加密后的字节区间。
5. 保持每段真实 `EXTINF`，每段正确的 IV，并以 `EXT-X-ENDLIST` 结束。

下面只是清单格式示例，签名均为占位符：

```m3u8
#EXTM3U
#EXT-X-VERSION:4
#EXT-X-TARGETDURATION:6
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

## 上传与发布

采用「先验证分片，最后发布清单与索引」顺序：

1. 在私有任务目录中保存任务 ID、源文件身份、片长、每包大小与 SHA-256、IV 和密钥备份。重试复用已核验包，避免覆盖同名既有影片。
2. 将加密包上传到自己的 AList 云盘挂载。比较上传后大小并抽查 Range 请求是否返回预期 206 和正确字节范围。
3. 从云端取样或全量读取加密字节，按对应 IV 解密并用 FFmpeg 解码验证。至少验证首段、末段和跨 pack 边界。
4. 在 AList 服务端通过 `POST /api/fs/get` 为密钥和各 pack 获取签名，构造 `/d/<路径>?sign=...` 或 `/p/<路径>?sign=...` 地址，并写进清单。分片和密钥不能依赖客户端追加 AList Authorization。
5. 在私有 NAS 媒体目录原子发布 `key` 和 `index.m3u8`；密钥权限仅开放给需要读取它的服务。
6. 用 `scripts/build_catalog.py` 更新索引。兼容服务每次读取当前索引，无需重启即可看到新影片。
7. 通过真实客户端验证首播、拖动和跨 pack 播放后，再按自己的备份策略清理临时数据。

**清单必须事先包含可访问的密钥与分片 URI。** 兼容服务只为入口清单调用 `fs/get`，对其内部链接做 `urljoin`；它不会重新签发内部签名。签名期限由 AList 控制，过期或路径更换后应重新发布清单。迁移挂载路径时保留 AES、IV、BYTERANGE 和片长，只改资源路径及相应签名。

无需 pack 的部署可以直接使用常规独立 TS 文件，省略 `BYTERANGE`。架构不绑定具体云盘品牌，存储的上传限制、Range 能力与代理行为应按真实挂载验证。

## 不包含的自动化

仓库没有内置云盘账号、真实媒体或自动删除原片的工具。元数据与封面可通过 [刮削 CLI](scraper.md) 补齐。自动清理流程应独立维护，并以云端可读、解密解码、清单发布和密钥备份均已验证为前提。
