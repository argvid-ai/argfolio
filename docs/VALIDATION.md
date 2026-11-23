# 验证记录

## 2026-11-23 本地重新运行

| 检查 | 结果 |
| --- | --- |
| `python3 -m unittest discover -s mobile_app/tests -v` | 9 项通过；覆盖鉴权、互斥、取消与重试、删除、任务恢复、结果发现和下载路径限制。 |
| `PYTHONPATH=pageflow/src python3 -m unittest discover -s pageflow/tests -v` | 34 项通过；覆盖选帧状态机、去重、配置、错误处理、清单和 PDF 导出。 |
| `python3 -m compileall -q mobile_app/backend mobile_app/desktop mobile_app/pipeline mobile_app/tools pipeline_v5 pageflow/src board_support` | 通过。 |
| `bash -n mobile_app/tools/install_board.sh mobile_app/tools/connect_workbench.sh mobile_app/desktop/build_deb.sh pipeline_v5/start_single_page_preview.sh` | 通过。 |
| `mobile_app/tools/build_android.py` 使用本机预览工具和本机签名材料构建 | 构建成功；APK 校验显示 v2/v3 签名有效，v1 未启用；产物仅留在忽略目录。 |
| 新 SSH 入口的非交互登录与工作台转发 | 登录成功；板端 8088、8089 健康接口及经本机 8088 转发的健康接口均返回 HTTP 200；本机工作台转发服务运行中。真实主机与端口仅保存在 Git 忽略的本地配置。 |

## 既有板端记录的摘要

- 14 页 T0 复核 PDF：记录中每页 `text_roundtrip_exact=true`，嵌入 JPEG 字节一致。
- 单页 V5 原版与第 86 页复核版：各 9 页，记录中 `all_body_pages_processed_by_model=true`、`text_roundtrip_exact=true`、`both_rendered=true`。
- 手机结果查看既有验证：板端结果列表含 14 页与两份 9 页文档；预览及下载成功，三种未鉴权访问返回 401，390 像素布局无溢出或脚本错误。

以上板端与手机结果是历史运行记录的摘要，并非本次在真实设备上重测。原始 PDF、录制、模型、完整日志和带设备地址的报告未纳入 GitHub 版。Android 实机交互、iOS Xcode 编译与实机验收仍待完成。
