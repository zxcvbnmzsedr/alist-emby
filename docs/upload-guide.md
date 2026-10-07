# 加密视频上传指导与 skill

阅读 [加密视频切片上传流程](../.agents/skills/alist-media-upload/references/upload-workflow.md)，包含格式约束、操作顺序、CLI、清单示例和真实验收范围。

[alist-media-upload skill](../.agents/skills/alist-media-upload/SKILL.md) 放在项目 `.agents/skills/`，指导代理复用本仓库工具完成上传、恢复与清单发布。支持项目 skill 的工具可直接发现该入口；需要全局安装时复制完整的 `alist-media-upload` 目录到自己的 skill 目录，保留 `references/` 和 `agents/`。

skill 不附带另一份应用代码。安装后仍需克隆 alist-emby 和配置依赖；全局使用时以 `ALIST_EMBY_REPO` 指定工程目录。仓库包含合成资料和环境模板，实际媒体、key、凭据与任务状态继续私有保存。
