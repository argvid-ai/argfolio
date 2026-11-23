# PageFlow

此目录保存既有 V4 基线中完成的翻页选帧源码、配置和测试。输入为翻页视频；`analyze` 输出稳定页面、清单和图片 PDF。后续单页 V5 流程调用该工具，再由 `board_support/audit.py` 独立核对翻页事件。

```bash
python3 pageflow/run_pageflow.py --help
PYTHONPATH=pageflow/src python3 -m unittest discover -s pageflow/tests -v
```

实际分析需要 OpenCV、NumPy、Pillow、ffmpeg 等运行依赖和视频输入。样本视频与结果未纳入公开仓库。原基线中的个别环境路径和设备地址未复制到本目录。
