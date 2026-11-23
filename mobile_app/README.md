# PaperScan 工作台与客户端

本目录包含板端 HTTP 工作台、网页界面、Android 与 iOS 工程、Linux 桌面客户端及板端安装工具。项目总览、依赖、构建、验证和公开仓库范围见根目录 `README.md`。

`backend/server.py` 管理配对、录制、任务、结果库和下载。可用 `--camera`、`--model`、`--runner`、`--results-root` 指定本地服务与处理程序。`web/` 是浏览器和移动端共用的界面。`pipeline/` 保存当前部署所用的板端单页流程副本，完整研发和历史辅助脚本在 `../pipeline_v5/`。

```bash
python3 mobile_app/backend/server.py --help
python3 -m unittest discover -s mobile_app/tests -v
```

`tools/install_board.sh` 安装结果查看服务。若需扫描控制，须按现场环境启用摄像头服务并配置 `--enable-capture`。桌面与手机转发 `.service` 文件从本机 `~/.config/paperscan/board-ssh.env` 读取 SSH 连接参数；参照 `tools/board-ssh.env.example` 配置，真实地址不进入 Git。自动重连要求非交互 SSH 登录。手机转发需另设可信局域网绑定地址，且不能与工作台转发争用本地 8088 端口。配对码由服务在本地首次运行时生成，应只通过私密渠道交付。

Android 预览构建须提供本机 Android 工具目录及 `PAPERSCAN_PREVIEW_KEYSTORE_PASSWORD` 环境变量。签名私钥和生成的 APK 均不得提交。iOS 工程须在 Mac 上用 Xcode 编译、签名和真机验收；仓库不包含 IPA。
