# Argfolio 离线书刊AI扫描机器人

此芯文档 PaperScan:板端单页扫描、文字整理和文档查看项目。仓库保留可审阅的源码、客户端工程、部署模板与验证摘要；原始视频、模型权重、文档内容、签名材料、设备地址及交付安装包留在本地。

## 已完成的部分

| 目录 | 内容 |
| --- | --- |
| `mobile_app/backend`、`mobile_app/web` | 配对鉴权、录制和处理任务、结果库、逐页预览、两种 PDF 与记录包下载。 |
| `mobile_app/android`、`mobile_app/ios`、`mobile_app/desktop` | Android、iOS 和 Linux 桌面客户端源码。iOS 工程仍需 Mac/Xcode 真机验收。 |
| `mobile_app/pipeline` | 当前板端工作台调用的单页处理流程副本。 |
| `pageflow`、`board_support/audit.py` | 从既有 V4 基线中选出的翻页选帧源码、测试和独立事件审计器。 |
| `pipeline_v5` | 单页采集、PP-OCRv5、MiniCPM3、PDF 导出及既往复核/打包脚本。后者包含特定历史任务的路径，需要按环境调整。 |
| `docs/VALIDATION.md` | 本次重新运行的检查和历史板端验证摘要。 |

## 环境与启动

板端处理依赖 CIX 板、摄像头、本仓库的 PageFlow 与 audit 程序、PP-OCRv5 ONNX 权重、RapidOCR/ONNX Runtime、MiniCPM3 本地服务、字体、ffmpeg 和 Poppler。权重及第三方程序不在仓库中，需由使用者自行取得并按许可证安装。`pipeline_v5/run_single_page_video.py` 和 `board_pipeline_v5.py` 中的板端路径是原部署布局，移植时请传参或调整本地路径。当前文字处理为 CPU 后端，双页扫描尚未实现。

仅查看已有结果时，在板端运行：

```bash
bash mobile_app/tools/install_board.sh
```

此脚本在用户目录下部署服务，首次运行会生成私有配对码文件。不要将配对码、含配对码的二维码或结果文档提交到公开仓库。若需录制与处理，先在板端部署相应依赖，再使用 `--enable-capture`、`--camera`、`--model`、`--runner` 配置 `mobile_app/backend/server.py`。使用 `python3 mobile_app/backend/server.py --help` 查看全部参数。默认摄像头和模型 URL 是板端回环地址，公开或远程服务地址必须由部署者自行配置并启用安全传输。

SSH 转发单元 `mobile_app/tools/cix-paperscan-phone.service` 与 `cix-paperscan-workbench-tunnel.service` 从 `~/.config/paperscan/board-ssh.env` 读取 SSH 用户、主机和端口；真实地址不在 Git 中。复制 `mobile_app/tools/board-ssh.env.example` 到该位置并填写，再将服务单元复制到 `~/.config/systemd/user/`，执行 `systemctl --user daemon-reload` 和 `systemctl --user enable --now cix-paperscan-workbench-tunnel.service`。自动重连要求非交互 SSH 登录及已核对的主机密钥。手机转发单元默认不启用，需先将 `PAPERSCAN_PHONE_BIND_IP` 设为可信局域网地址，并避免与工作台转发使用同一本地端口。终端手动转发可运行 `mobile_app/tools/connect_workbench.sh`。`mobile_app/tools/cix-paperscan-workbench.service` 是板端本机回环服务模板，也需核对本机目录和端口。

## 客户端构建

- **Android：** `mobile_app/android` 是 Android Studio 工程，目标 SDK 35、最低 Android 7.0。`mobile_app/tools/build_android.py` 可使用本机 aapt2、D8、apksig 构建预览包；先设置 `PAPERSCAN_PREVIEW_KEYSTORE_PASSWORD`，并提供 `--tools`、`--output`。`--workbench-url` 可在构建时指定自己的 HTTPS 入口。生产发布应使用自己的正式签名与真机验收。
- **iOS：** 在 Mac 上用 Xcode 打开 `mobile_app/ios/PaperScan.xcodeproj`，设置自己的 Team 和 Bundle Identifier，编译并真机测试。当前仅提供工程，没有经 Xcode 验证的 IPA。
- **Linux 桌面：** `mobile_app/desktop/app.py` 是桌面工作台客户端，`build_deb.sh` 是打包入口；默认访问本机 SSH 转发端口，可在客户端修改地址。

## 检查

网关回归使用本机模拟服务。要完整运行 PageFlow 测试，可先在独立 Python 3.12 虚拟环境中执行 `python -m pip install opencv-python-headless numpy pillow`。板端 OCR、相机、模型服务与客户端构建各有独立的运行依赖，见上方环境说明。

```bash
python3 -m unittest discover -s mobile_app/tests -v
PYTHONPATH=pageflow/src python3 -m unittest discover -s pageflow/tests -v
python3 -m compileall -q mobile_app/backend mobile_app/desktop mobile_app/pipeline mobile_app/tools pipeline_v5 pageflow/src board_support
bash -n mobile_app/tools/install_board.sh mobile_app/tools/connect_workbench.sh mobile_app/desktop/build_deb.sh pipeline_v5/start_single_page_preview.sh
git status --short --untracked-files=all
```

2026-11-23 重新运行的 9 项工作台集成测试、34 项 PageFlow 测试、编译和 shell 语法检查均通过。板端历史验证与本次检查的边界见 [`docs/VALIDATION.md`](docs/VALIDATION.md)。Android 真机交互、iOS 编译/真机验收以及在新设备上完整重跑模型流程尚未完成。

## 提交范围和敏感配置

`.gitignore` 排除 `archives/`、`runs/`、`output/`、`tmp/`、T0 复核交付、安装包、密钥、证书和本地环境文件。这些目录含大体量或特定用户的原始材料，GitHub 版只保留经过归纳的验证结果。提交前核对 `git status --short --untracked-files=all`，再执行 `git add .`。所有新增的真实服务器地址、API key、SSH 私钥、配对码、签名私钥与用户文档必须留在仓库外；服务入口和凭据用部署参数或本地环境变量配置。

## 软件许可与商业使用

本项目有权授权的原创软件采用 [PolyForm Noncommercial License 1.0.0](LICENSE)。在许可规定的用途内，可以使用、修改和分发软件。商业产品集成、收费服务等超出许可范围的用途，请事先向相应权利人取得单独的书面商业授权。

第三方软件与工具、OCR 和语言模型权重、图标及其他素材，按相应权利人的条款使用或分发。 商标与标识的使用须遵循权利人要求。
