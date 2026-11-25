# 开发与构建

本源码候选面向 Windows x64。核心来源提交为 `e9d22a9cf20e419f45b5d1a3602e6286481670be`；公开导出移除了机器专属回退、私有路径和内部记录，将测试目录改为可配置的隔离目录，并更新了下述 PyAV 安全补丁锁与参数化封包输入。具体文件来源与修改状态见 `PUBLIC_SOURCE_MANIFEST.json`。

## 开发环境

使用独立虚拟环境。根 `requirements.txt` 是兼容旧开发入口的下限依赖清单，不是完整录音/ASR环境锁；`baseline/requirements-runtime.lock.txt` 和 wheel/runtime/assets 锁描述既有 Windows 安装闭包。前端在 `web/` 中使用 `npm ci`、`npm run build`；锁定构建依赖在 `web/package-lock.json`。

原基线的 PyAV 18.1.0 已被 PyPI 撤回，原因涉及音频解码 double free。本源码候选将单包锁更新为官方 PyAV 18.1.1（Windows x64、cp311-abi3），并同步更新 wheel 哈希、requirements 和运行锁绑定。CPython 3.14.10 的隔离 CPU 验证通过7类合成音频检查：WAV解码/重采样、真实 faster-whisper audio.py 消费、立体声拆分、FLV/AAC往返、坏媒体拒绝及32次资源清理。没有构造模型、访问平台、使用真实媒体或重建完整安装包；不能将这些定向结果当成全格式安全证明或新的干净机安装验收。

## 隔离测试

无需安装模型或访问平台的最小标准库检查为：

```powershell
python -B scripts/check_public_source.py
python -B -m unittest discover -s scripts/local_data/tests -v
```

后一组覆盖7项合成资料投影和索引回归，包括文件掩码、日期窗口、尾部未完成记录、坏尾注、重叠窗口、检查期间变更和失败时保留原输出。它不覆盖真实采集、模型、安装、平台登录或长期运行。

测试只应使用合成数据。部分涉及本机删除、安装或派生材料回收的用例保留了显式目录归属检查。先选一个新建且完全由本次测试拥有的目录，设置 `DLI_TEST_ROOT`，并给 pytest 显式指定该目录下的 `tmp-parent` 为 `--basetemp`。不要把真实运行目录用于测试。

```powershell
$env:DLI_TEST_ROOT = Join-Path $env:TEMP 'douyin-live-insight-tests-new'
python -m pytest tests/test_audio_cleanup.py --basetemp "$env:DLI_TEST_ROOT/tmp-parent"
```

只运行所选文件对应的隔离组；一些旧专用审计测试会安装进程级审计钩子，不能据一次任意全量执行就声称已完成整个安装器/平台验收。

## 安装闭包

相关脚本均要求显式输入和新的输出目录，不扫描或复制用户运行状态：

1. 按 `baseline/python-runtime-lock.json`、`python-wheel-lock.json`、`customer-assets.lock.json` 准备对应官方资产和离线缓存，保留许可证。构建 Python 需单独准备 pip，客户运行包不现场 pip 安装。
2. 查看 `scripts/prepare_customer_python.py --help`、`scripts/prepare_customer_assets.py --help`，在隔离构建目录准备运行时和资产。
3. `scripts/freeze_customer_program.py` 根据显式入口、静态本地导入和资源白名单生成程序锁；仓库克隆中可使用 Git 文件清单，导出目录可使用 `--source-manifest PUBLIC_SOURCE_MANIFEST.json`。输出必须是新文件。
4. `scripts/build_customer_payload.py` 和 `scripts/build_customer_bundle.py` 消费明确锁和输入。使用新生成的 `--program-lock`；不要把历史锁误当修改后源码的字节证明。
5. `packaging/` 在给定的可信 bundle 上绑定新的 manifest/setup 摘要并构建桌面入口及外层包。

各脚本的 `--help` 列出当前参数。内部安装事务版本/channel是兼容协议，不能只为匹配 GitHub Release 标签而改名。公开 Beta 应来自可追溯的构建，完成资产与源码校验、适用的定向检查，并具备实际再分发所需的许可、notice 和相应源码材料。

发布说明应分别列明此前13项核心/安装验收、本轮桌面包装合成检查和 PyAV 定向音频检查。新构建完整安装包的整机试装若尚未进行，应明确写为公测待反馈范围，不能冒称通过，也不把每个新 Beta 的全量干净机试装设为统一前置条件。

`PUBLIC_SOURCE_MANIFEST.json` 是公开源码清单，不包含 `.git`、账号、登录态、真实名册、数据、模型权重、安装器 EXE、依赖 wheel 或旧研究抓包。第三方来源和完整许可见 `THIRD_PARTY_NOTICES.md`、`LICENSE` 及 `dependency-licenses/`。
