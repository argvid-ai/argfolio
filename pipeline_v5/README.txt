V3 单页版：板端 PP-OCRv5 + MiniCPM3 正文处理

处理范围：竖向单页录制。摄像头原始采集 3840×2160@30，中央 1536×2160 用于单页录制；完整 4K 采集选项不代表支持双页 OCR。双页分割、书脊矫正、左右页顺序及逐页质检尚未实现。

流程：PageFlow 选帧与质检 → 板端 PP-OCRv5 mobile det/cls/rec → 主文字列与相邻重复页筛选 → 板端 MiniCPM3 段落规划及最多两处受限校正 → 文字 PDF 与可搜索原图 PDF → 文字提取和渲染校验。OCR 后端为 ONNX Runtime CPUExecutionProvider，不能称作 V5 NPU。

运行入口：
  python3 run_single_page_video.py /path/to/portrait-video.mp4
  python3 board_pipeline_v5.py --source /path/to/pageflow-workspace --output /path/to/result --endpoint http://127.0.0.1:8090 --label scan

上述回环地址仅是板端本机模型服务示例；远程模型地址须由操作者显式配置。模型权重、PageFlow、audit.py、字体、ffmpeg、RapidOCR、PDF 工具等依赖须在板端单独安装。原始运行记录和模型不在 Git 仓库中。详见根目录 README。
