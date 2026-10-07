# 刮削、NFO 与封面

## 安装

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-scraper.txt
```

工具面向 macOS / Linux，需要文件锁支持。截帧另需系统 FFmpeg。JavTrailers/MissAV provider 使用 `curl_cffi`；依赖版本范围由 `requirements-scraper.txt` 统一管理。

当前流程调用随仓库提供的 JavBus 和 JavTrailers provider，编号格式为 `TEST_001` 或 `TEST-001` 等字母与数字编号。两个来源均成功时，编号、发布日期、时长和演员必须一致；一个来源完整成功时可采用该来源。无完整结果时不写入猜测资料。

## 查询与导入

```sh
# 预览，不写媒体目录。
.venv/bin/python cinema/scripts/import_metadata.py TEST_001 \
  --media-root /srv/media-hls --output ./catalog

# 写入 NFO、按需封面、刷新索引。
.venv/bin/python cinema/scripts/import_metadata.py TEST_001 --apply \
  --media-root /srv/media-hls --output ./catalog

# 只导入文字，不下载封面或截帧。
.venv/bin/python cinema/scripts/import_metadata.py TEST_001 --apply --skip-cover \
  --media-root /srv/media-hls --output ./catalog
```

目录名与编号不同，可以使用 `--code TEST-001` 显式指定；目录名仍必须是媒体根目录下的一个普通目录。已有标题、简介、分类、封面以及演员附加信息保留。新 NFO 的标题采用编号；补齐演员、导演、片商、日期和来源片长。播放界面的片长仍优先使用真实 HLS 清单累计值。

图片只接受通过完整解码校验的静态 JPEG/PNG/WebP，限制 10 MB、最多 2500 万像素，并记录 SHA-256。不替换已有封面。工具不会下载视频或剧情描述，不改清单和密钥。

写入前保存候选 NFO、旧 NFO、旧索引及核验报告到 `state/scraper/imports/`。若索引更新失败，恢复旧 NFO 与索引并移除本次新增封面。

来源站可能要求年龄确认或返回 403、验证码及限速页面。来源不通时报告失败，自动封面模式可转为截帧；网站可用性不等于本地测试通过。

## 本地 JSON 与远端查询结果

`examples/metadata.json` 给出了仅包含合成资料的格式。必须包含 `facts`；编号要与 `--code` 或目录编号一致，并且有有效日期、演员和片商。

```sh
.venv/bin/python cinema/scripts/import_metadata.py example --code TEST-001 \
  --metadata-json examples/metadata.json --apply --skip-cover \
  --media-root /srv/media-hls --output ./catalog
```

`--verified-stdin` 可以从标准输入接收同样结构，用于自己维护的 SSH 查询/导入流程。这里的“verified”表示调用者准备并确认了输入，工具仍检查字段、编号和图片完整性，不替调用者确认资料来源。来源名支持 `javbus`、`javtrailers`、`local`；可选 `poster` 使用包含 base64 图像、尺寸、格式、SHA-256 与 source 的负载结构。

## 自动补封面

```sh
.venv/bin/python cinema/scripts/import_metadata.py --list-missing-covers --media-root /srv/media-hls
.venv/bin/python cinema/scripts/import_metadata.py --auto-cover \
  --media-root /srv/media-hls --output ./catalog
.venv/bin/python cinema/scripts/import_metadata.py TEST_001 --auto-cover \
  --media-root /srv/media-hls --output ./catalog
```

顺序是已有封面跳过 → 刮削资料和封面 → 仍缺封面时取第 3 秒。已有封面时不读取 AList 凭据、不调用 FFmpeg、不写媒体目录或刷新索引。批量逐部保存结果，单部失败不阻断其他影片；重跑只处理仍缺封面的影片，存在失败时命令返回非零。

截帧使用以下环境配置：

| 变量 | 用途 |
|---|---|
| `ALIST_ORIGIN` | NAS/工具访问 AList 的地址，默认 `http://127.0.0.1:5244` |
| `ALIST_TOKEN` | 本地环境提供的 AList API Token，不写报告或日志 |
| `ALIST_DATABASE` | 未配置 Token 时，从指定 SQLite 只读取得管理 API Token |
| `ALIST_SEGMENT_ROOT` | 云端分片所在 AList 根目录，默认 `/cloud/raw` |

凭据放在自己的受限环境文件或运行环境中，不提交到仓库。截帧在本机临时清单中重新取得资源签名，保留 AES、IV 和 BYTERANGE，并限制密钥和分片属于当前影片。既支持 `/p/<分片根>/<影片>/...` 的远端分片，也支持媒体目录中的相对 TS 或 `/d/m3u8/<影片>/...`。不修改原清单。

## 手动选帧

```sh
# 默认第 3 秒，仅生成预览。
.venv/bin/python cinema/scripts/import_metadata.py TEST_001 --frame \
  --media-root /srv/media-hls --output ./catalog

# 指定时间，例如第 30 秒。
.venv/bin/python cinema/scripts/import_metadata.py TEST_001 --frame 00:00:30 \
  --media-root /srv/media-hls --output ./catalog

# 发布返回的 preview_id 对应的那一张图片。
.venv/bin/python cinema/scripts/import_metadata.py --apply-preview PREVIEW_ID \
  --media-root /srv/media-hls --output ./catalog
```

预览和报告位于 `state/scraper/frame-covers/<ID>/`。发布前比较图片和清单哈希，发生变化则拒绝；已有封面仍跳过。发布失败时撤回新封面并恢复索引。`--frame ... --apply` 可以直接发布这一帧，`--cover-time` 用于覆盖自动模式的默认时间。
