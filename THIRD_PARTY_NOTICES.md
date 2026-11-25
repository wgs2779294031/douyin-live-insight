# 第三方来源与许可说明

本文件记录已核实的第三方来源及发布材料的准备状态。第三方组件的版权和适用许可仍归其各自权利人；本项目的许可不能替代或取消这些条款。本文件不表示全部依赖的许可材料已经归档完成。

## DouyinLiveWebFetcher

本项目使用了 [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) 的代码。来源核对固定在提交 [`40a904bdb0561a5a9c958a95634a29681cc4fbf4`](https://github.com/saermart/DouyinLiveWebFetcher/tree/40a904bdb0561a5a9c958a95634a29681cc4fbf4)。

上游仓库提供 [GNU Affero General Public License version 3](https://github.com/saermart/DouyinLiveWebFetcher/blob/40a904bdb0561a5a9c958a95634a29681cc4fbf4/LICENSE)。本目录的 [LICENSE](LICENSE) 是该固定提交的完整许可原文，未作修改。请保留第三方来源、版权及许可说明，并按适用条款提供相应源码。

以下是准备开源时核对的源码基线；核对结果不代表这些文件已经包含在某个公开版本中。具体版本应以其文件清单和提交为准。

| 文件 | 与固定上游的核对结果 |
| --- | --- |
| `a_bogus.js` | Git 内容对象完全相同，未发现本地改动 |
| `sign.js` | Git 内容对象完全相同，未发现本地改动 |
| `sign_v0.js` | Git 内容对象完全相同，未发现本地改动 |
| `webmssdk.js` | Git 内容对象完全相同，未发现本地改动 |
| `ac_signature.py` | Git 内容对象完全相同，未发现本地改动 |
| `protobuf/douyin.py` | Git 内容对象完全相同，未发现本地改动 |

签名封装 `sign.py` 的注释明确参考上游 `liveMan.py`；该封装的全部派生及修改范围尚未逐行核对，不能将其全部标为独立原创。`protobuf/douyin.proto` 的当前本地内容不同于该上游提交根目录的同名协议文件，其来源演变和修改范围也尚未完成核查。

上游 [README](https://github.com/saermart/DouyinLiveWebFetcher/blob/40a904bdb0561a5a9c958a95634a29681cc4fbf4/README.MD) 另有学习研究用途、非商业谋利及禁止违法滥用的声明。该说明与仓库标准 AGPL 文本并存，本文件仅记录并链接这一情况，不对其法律效力或优先关系作判断，也不将其改写为本项目新增的非商业许可条款。此来源记录不构成对所有商业用途的授权保证。

## 运行时、模型与前端依赖

安装发行物包含的 Python、Node.js、FFmpeg、语音模型及其他依赖应保留各自的完整许可和必要 notices。已检查的安装候选保留了 FFmpeg、Node.js、Whisper 相关材料、CPython 许可，以及 ONNX Runtime 的 `LICENSE` 和 `ThirdPartyNotices.txt`；这些组件不因本项目许可而改变自身许可。

以下附件仍是发布准备中的明确待办，不能将本说明当成已经补齐的证明：

| 组件 | 核对版本 | 需要补齐的材料 |
| --- | --- | --- |
| CTranslate2 | 4.9.0 | 对应版本的完整版权、许可及必要 notices |
| FlatBuffers | 25.12.19 | 对应版本的完整版权、许可及必要 notices |
| Tokenizers | 0.23.3 | 对应版本的完整版权、许可及必要 notices |
| Vue | 3.5.44 | 完整 MIT 许可和版权说明；现有构建保留了许可横幅 |
| Vue Router | 4.6.4 | 完整 MIT 许可和版权说明；现有构建保留了版权横幅 |

PyAV、NumPy 及其他原生依赖中捆绑的库，还需按实际发行文件核对来源、构建选项、完整 notices 和适用的源码或再链接要求。未列出的组件不因此视为无须履行许可义务。

## 记录范围

核对日期：2026-11-26。以上内容是可追溯的来源记录与待办说明，不是完整权属或许可合规认证。公开版本有组件增删或修改时，应同步更新本文件、相应版权声明和随包许可材料。
