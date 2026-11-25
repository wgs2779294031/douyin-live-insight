# 源码检查

`Source checks` 工作流在提交到 `main`、向 `main` 提交或更新 PR，以及手动触发时运行。它使用 GitHub 托管的 Windows 运行器和 Python 3.14，只授予仓库内容读取权限，不保存 checkout 凭据，不接收或配置项目密钥。

当前检查包括：

- `scripts/check_public_source.py`：检查 Git 跟踪文件的公开路径、文本编码、Python 语法和 JSON 格式，不导入应用模块。
- `scripts/local_data/tests`：运行7项标准库合成回归，检查资料投影、日期窗口、脱敏、索引及失败时保留原输出。
- `packaging/tests/test_packaging.py`：运行标准库合成 ZIP 回归，检查正式清单的四字段、组件角色与路径、摘要、目录及输出保护。显式设置 `RUN_DESKTOP_HARNESS=0`，跳过可选的原生编译与桌面 harness；临时文件只写入本轮运行器临时目录。

工作流不安装应用依赖，不运行模型、真实采集、平台登录、安装器或发布任务。它没有 `pull_request_target` 触发器，外部 PR 仍受 GitHub 对首次贡献者和工作流执行的审批规则约束。

这些检查通过，表示该提交通过了上述限定范围。它不等同于前端构建、完整安装包、真实媒体、平台兼容或长期运行已经验收。其他维护应按修改内容补充适当验证，见 [开发与构建](BUILDING.md)。

## 官方 Actions 固定版本

核对日期：2026-11-26。工作流使用完整提交 SHA，避免标签后续移动改变已审查的依赖：

| Action | 核对的正式版本 | 固定提交 |
| --- | --- | --- |
| [actions/checkout](https://github.com/actions/checkout/releases/tag/v7.0.1) | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| [actions/setup-python](https://github.com/actions/setup-python/releases/tag/v7.1.0) | v7.1.0 | `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` |

固定提交来自各官方仓库对应正式版本的 Git 引用。升级时应重新核对官方发布、运行器要求及适用检查，不直接替换为未审查的可变标签。
