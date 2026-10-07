# 随项目提供的元数据 provider

来源：[acer1204/jav-metadata-syncer](https://github.com/acer1204/jav-metadata-syncer)。

基准提交：`2e6db76c8f335753dba167071b3be0e5d93667b2`。

此目录保留现有方案使用的 `backend/` 源码，不包含用户配置、账号、抓取缓存或媒体。`alist-emby` 的 `cinema/scripts/import_metadata.py` 仅使用 `backend/app/clients` 作为 provider 库，默认调用 JavBus 和 JavTrailers，不启动上游网页、数据库或 HTTP 服务。MissAV provider 源码也保留在上游目录中，默认导入流程不调用它。

`requirements-scraper.txt` 提供 CLI 所需的依赖；上游 `backend/requirements.txt` 是完整后端的依赖清单。本目录中的上游文件保留原内容，项目根目录的 MIT 声明针对本项目自身代码和文档，不更改这些文件的来源。
