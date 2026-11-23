"""Refresh V3 with native capture runs; execute on the board."""
from pathlib import Path
import datetime
import hashlib
import json
import shutil
import zipfile

home = Path('/home/radxa')
root = home / 'tu/releases/Phecda_Scan_V3_20261107_SinglePage_V5'
archive = root.with_suffix('.zip')
stamp = datetime.datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')
if archive.exists():
    shutil.copy2(archive, archive.with_name(archive.stem + '_BeforeNativeUpdate_' + stamp + '.zip'))
latest_name = 'Phecda_Run_20261107_164833_Reselected86'
for name in ['Phecda_Run_20261107_164833_SinglePage_V5', latest_name]:
    src = home / 'tu/deliveries' / name
    assert src.is_dir(), src
    shutil.copytree(src, root / 'runs' / name, dirs_exist_ok=True)
latest = root / 'runs' / latest_name
version = json.loads((latest / '运行记录/version.json').read_text())
version.update(archive_updated_at=datetime.datetime.now().astimezone().isoformat(),
               latest_run='runs/' + latest_name,
               release_revision='native_single_page_reselected86',
               historical_comparison={'results_directory': 'results',
                   'input': 'EMEET-REC-20261107-055911.mp4', 'pdf_pages': 14})
(root / 'version.json').write_text(json.dumps(version, ensure_ascii=False, indent=2))
for name in ['board_pipeline_v5.py', 'run_single_page_video.py', 'web_cam_singlepage.py',
             'reselect_native_86.py', 'package_reselected86.py', 'update_v3_archive.py']:
    shutil.copy2(home / 'tu/scripts/v5' / name, root / 'code/pipeline_v5' / name)
(root / 'code/pipeline_v5/version.json').write_text(json.dumps(version, ensure_ascii=False, indent=2))
shutil.copy2(home / 'Project/webcam-live/web_cam_singlepage.py', root / 'code/webcam-live/web_cam_singlepage.py')
readme = root / 'README_PACKAGE.txt'
old_readme = readme.read_text()
backup = root / 'provenance/README_PACKAGE_before_native_update.txt'
if not backup.exists():
    backup.write_text(old_readme)
readme.write_text('''Phecda Scan V3 单页版：最新板端原生录制及第86页复核修订

最新结果入口：runs/Phecda_Run_20261107_164833_Reselected86/
包含最终9张选帧、两份9页PDF、V5 OCR、MiniCPM3记录、渲染、audit和选帧复核记录。
第86页已替换为17.0秒f000510，改善正文完整性；第90页保留45.4秒f001362，手指遮挡前两行仍存在。
V5与MiniCPM3正文处理和PDF生成均在板端完成，本机仅复核与传输。
V5使用板端CPU ONNX Runtime，MiniCPM3使用板端CPU；未宣称V5已上NPU。
单页采集为中央1536×2160@30fps。双页流程尚未实现，需要独立配置和处理模块。

runs/Phecda_Run_20261107_164833_SinglePage_V5/ 保留本次录制最初交付。
顶层results/和data/保留早期Windows素材的14页对照实验，最新结果请从上述runs目录进入。
原始本次视频仍位于板端~/tu/recordings/c60e_20261107/rec_20261107_164833.mp4，输入哈希见运行记录。
模型、环境锁定、完整采集和处理代码继续保存在本V3包内。

以下为历史对照实验说明：
''' + old_readme)
for name, source in [('native_run_pipeline.log', home / 'tu/logs/rec_20261107_164833_full_pipeline.log')]:
    if source.exists():
        shutil.copy2(source, root / 'logs' / name)
checks = {str(p.relative_to(root)): {'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
          for p in root.rglob('*') if p.is_file() and p != root / 'PACKAGE_MANIFEST.json'}
(root / 'PACKAGE_MANIFEST.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2))
temporary = archive.with_suffix('.zip.tmp')
with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
    for path in root.rglob('*'):
        if path.is_file():
            z.write(path, path.relative_to(root.parent))
with zipfile.ZipFile(temporary) as z:
    assert z.testzip() is None
temporary.replace(archive)
print(json.dumps({'archive': str(archive), 'size': archive.stat().st_size,
                  'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                  'latest_run': version['latest_run'], 'files': len(checks) + 1}, ensure_ascii=False))
