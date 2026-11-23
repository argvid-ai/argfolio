"""Archive the completed single-page release on the board."""
from pathlib import Path
import json,shutil,hashlib,zipfile,subprocess,datetime

home=Path.home();workspace=home/'tu/workspace/emeet_20261107_v5_minicpm3'
run=json.loads((workspace/'run_manifest.json').read_text())
selection=json.loads((workspace/'selection.json').read_text())
assert selection['book_pages']==list(range(32,46)),selection['book_pages']
assert run['all_body_pages_processed_by_model'] and run['exports']['pages']==14
assert run['processing_page_mode']=='single_page'
root=home/'tu/releases/Phecda_Scan_V3_20261107_SinglePage_V5'
root.mkdir(parents=True,exist_ok=True)
def copy(src,dst):
    src=Path(src);dst=root/dst;dst.parent.mkdir(parents=True,exist_ok=True)
    if src.is_dir():shutil.copytree(src,dst,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    else:shutil.copy2(src,dst)
copy(workspace,Path('results'))
copy(home/'tu/scripts/v5',Path('code/pipeline_v5'))
for name in ['web_cam_singlepage.py','mjpeg_to_i420.c','mjpeg_to_nv12']:
    copy(home/'Project/webcam-live'/name,Path('code/webcam-live')/name)
copy(home/'tu/model/ppocrv5_mobile',Path('models/ppocrv5_mobile'))
copy(home/'Project/pp-ocr-v4/simfang.ttf',Path('assets/simfang.ttf'))
copy(home/'tu/scripts/v5/version.json',Path('version.json'))
source=home/'tu/workspace/emeet_20261107'
copy(source/'frames/crop',Path('data/frames/crop'))
copy(source/'manifest.json',Path('data/manifest.json'))
for name in ['single_page_smoke_test.json','version.json','camera_probe.jpg']:
    p=home/'tu/recordings/c60e_20261107'/name
    if p.exists():copy(p,Path('capture')/name)
smoke=json.loads((home/'tu/recordings/c60e_20261107/single_page_smoke_test.json').read_text())
clip=home/'tu/recordings/c60e_20261107'/smoke['stop']['file']
for p in [clip,Path(str(clip)+'.capture.json')]:
    if p.exists():copy(p,Path('capture')/p.name)
for name in ['pipeline_v5_20261107.log','install_v5_20261107.log','webcam_singlepage_20261107.log']:
    copy(home/'tu/logs'/name,Path('logs')/name)
prov=root/'provenance';prov.mkdir(exist_ok=True)
(prov/'requirements-v5.txt').write_text(subprocess.check_output([str(home/'tu/venv_v5/bin/python'),'-m','pip','freeze'],text=True))
video=home/'tu/recordings/emeet_20261107/EMEET-REC-20261107-055911.mp4'
(prov/'input_reference.json').write_text(json.dumps({'video':str(video),'sha256':hashlib.file_digest(video.open('rb'),'sha256').hexdigest(),
    'full_video_in_this_package':False,'full_video_package':'Phecda_Scan_V2_20261107_V4_Baseline.zip',
    'comparison_input':'Old Windows single-page video; different from the new native 1536x2160 camera crop'},ensure_ascii=False,indent=2))
old={r['order']:r for r in map(json.loads,(source/'ocr.jsonl').read_text().splitlines())}
new=[json.loads(l) for l in (workspace/'ocr_v5.jsonl').read_text().splitlines()]
comparison={'note':'Confidence is a model score, not character accuracy; no CER ground truth benchmark completed.',
    'v4_ocr_seconds':sum(r.get('ocr_seconds',0) for r in old.values()),'v5_ocr_seconds':sum(r['seconds'] for r in new),
    'body_model_pages':run['body_pages_processed'],'model_request_attempts':run['model_request_attempts'],
    'final_applied_model_edits':run['applied_edits'],
    'pages':[{'source_order':r['order'],'v4_mean_score':old[r['order']]['mean_score'],'v5_mean_score':r['mean_score'],
              'v4_first_text':''.join(i['text'] for i in old[r['order']]['items'])[:60],
              'v5_first_text':''.join(i['text'] for i in r['body_items'])[:60]} for r in new]}
(prov/'v4_v5_comparison.json').write_text(json.dumps(comparison,ensure_ascii=False,indent=2))
(root/'README_PACKAGE.txt').write_text('''Phecda Scan V3 单页版 PP-OCRv5 与 MiniCPM3 正文处理
版本日期 2026-11-07

本版明确为单页流程，后续双页版需独立增加处理模块；完整4K取景不等于已支持双页OCR。
摄像头采集3840×2160@30；当前默认中央1536×2160单页预览和实际录制，裁剪坐标1152,0,1536,2160，不添加黑边。
capture包含真实短录制及ffprobe尺寸检查。version.json记录采集、处理和模型模式以及未来双页边界。

模型流程
PP-OCRv5 mobile det/cls/rec -> 去重与主页面文字筛选 -> MiniCPM3正文分段及受约束局部校正 -> 文字PDF与可搜索原图PDF。
V5当前在此芯板CPU上以ONNX Runtime运行，未宣称CIX NPU加速。V4 NPU基线已单独封存。
MiniCPM3在板端CPU执行，逐页输入输出、方案、被采用和被拒绝改动均保留。纯文本小模型提出的不可靠改写被过滤，高置信OCR正文保持原样。
正文只来自板端模型，没有引入聊天转录或本机目视校订正文。

本轮结果
results中两份PDF均14页，覆盖书内32至45页；17张选帧的3组重复已去重。
文字PDF经过正文模型分段，原图PDF文本层保留V5识别结果。两份PDF均在板上生成、验证提取、渲染。
这次对照使用旧100.2秒Windows单页素材，不把它混记为本次摄像头新录素材。
OCR仍有漏字、错字；模型置信度提升不等于已测字符准确率。不可靠模型改字没有直接写进成品。

目录
results/        原始V5 OCR、MiniCPM3请求响应、最终正文、PDF与渲染、运行记录
code/           板端工具代码和运行说明
models/         本轮PP-OCRv5 ONNX模型及部署记录
data/           本轮17张原始裁剪选帧与PageFlow清单
capture/        单页采集配置、短录制测试和尺寸证据
provenance/     环境锁定、输入引用、V4/V5对照记录
assets/         PDF字体

原始100.2秒视频及更早完整流程在桌面V2基线包中；MiniCPM3约2.4GB权重不重复收录，仍位于板上~/tu/model。
启动采集和录制后处理命令见code/pipeline_v5/README.txt。
''')
files={str(p.relative_to(root)):{'size':p.stat().st_size,'sha256':hashlib.file_digest(p.open('rb'),'sha256').hexdigest()}
       for p in root.rglob('*') if p.is_file() and p.name!='PACKAGE_MANIFEST.json'}
(root/'PACKAGE_MANIFEST.json').write_text(json.dumps(files,ensure_ascii=False,indent=2))
archive=root.with_suffix('.zip')
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
    for p in root.rglob('*'):
        if p.is_file():z.write(p,p.relative_to(root.parent))
with zipfile.ZipFile(archive) as z:assert z.testzip() is None
print(json.dumps({'archive':str(archive),'size':archive.stat().st_size,'files':len(files)+1,
                  'sha256':hashlib.file_digest(archive.open('rb'),'sha256').hexdigest()},ensure_ascii=False))
