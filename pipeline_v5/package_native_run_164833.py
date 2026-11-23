from pathlib import Path
import json,shutil,hashlib,zipfile,subprocess
from pypdf import PdfReader
from PIL import Image,ImageDraw,ImageFont

home=Path.home();run=home/'tu/workspace/rec_20261107_164833_single_page_v5'
res=run/'results';frames=run/'frames_workspace'
meta=json.loads((res/'run_manifest.json').read_text())
selection=json.loads((res/'selection.json').read_text())
manifest=json.loads((frames/'manifest.json').read_text())
assert json.loads((res/'status.json').read_text())['stage']=='complete'
assert meta['body_pages_processed']==9 and meta['all_body_pages_processed_by_model']
root=home/'tu/deliveries/Phecda_Run_20261107_164833_SinglePage_V5';root.mkdir(parents=True,exist_ok=True)
def copy(src,dst):
    src=Path(src);dst=root/dst;dst.parent.mkdir(parents=True,exist_ok=True)
    if src.is_dir():shutil.copytree(src,dst,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    else:shutil.copy2(src,dst)
copy(frames/'frames',Path('选帧/全部'))
by_order={p['order']:p for p in manifest['pages']}
for i,(order,book) in enumerate(zip(selection['selected_orders'],selection['book_pages']),1):
    p=by_order[order];label=f'书页{book}' if book is not None else '页码未识别'
    copy(frames/p['crop_image'],Path('选帧/最终')/f'pdf{i:02d}_{label}_原p{order:02d}_f{p["frame_index"]:06d}.jpg')
pdfs=[]
for key in ['text_pdf','searchable_pdf']:
    pdf=Path(meta['exports'][key]);assert len(PdfReader(pdf).pages)==9
    copy(pdf,Path('PDF')/pdf.name);pdfs.append(pdf.name)
for p in res.glob('*正文.txt'):copy(p,Path('文本')/p.name)
for p in res.iterdir():
    if p.is_file() and p.suffix=='.json' or p.is_file() and p.suffix=='.jsonl':copy(p,Path('运行记录')/p.name)
for name in ['llm_raw','render_text','render_search']:
    copy(res/name,Path('运行记录')/name)
for name in ['manifest.json','metrics.csv','audit.json']:copy(frames/name,Path('运行记录/PageFlow_audit')/name)
for name in ['input.json','version.json','pageflow_single_page.json']:copy(run/name,Path('运行记录')/name)
copy(home/'tu/logs/rec_20261107_164833_full_pipeline.log',Path('运行记录/pipeline.log'))
copy(home/'tu/recordings/c60e_20261107/rec_20261107_164833.mp4.capture.json',Path('运行记录/capture.json'))
for name in ['board_pipeline_v5.py','run_single_page_video.py']:copy(home/'tu/scripts/v5'/name,Path('运行记录/code')/name)
body=[json.loads(l) for l in (res/'body_minicpm3.jsonl').read_text().splitlines()]
review={'source_video':'rec_20261107_164833.mp4','processing_location':'CIXPhecdaBoard',
    'pageflow_selected_frames':len(manifest['pages']),'unique_pdf_pages':9,
    'duplicates':selection['duplicate_groups'],'selection':selection,
    'audit_status':json.loads((frames/'audit.json').read_text())['verdict'],
    'visual_observations':[
        {'source_order':7,'issue':'A finger overlaps printed body text in the selected image.'},
        {'source_orders':[1,2,3],'issue':'Page edges or printed page numbers approach or exceed capture bounds. Printed numbers for final pages 2 and 3 were not read; no numbers were invented.'}],
    'text_source':'Board PP-OCRv5 and board MiniCPM3; no chat transcription supplied to the documents',
    'body_pages_processed_by_model':len(body),'accepted_edits':sum(len(p['applied_edits']) for p in body),
    'model_device':'PP-OCRv5 ONNX Runtime CPU; MiniCPM3 CPU on board',
    'accuracy_limit':'OCR can contain errors and cannot recover finger-occluded or uncaptured text; confidence is not measured character accuracy.'}
(root/'运行记录/quality_review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2))
(root/'先读我.txt').write_text('''本次单页视频处理结果 2026-11-07 16:48:33

输入：板端原生单页录制rec_20261107_164833.mp4，1536×2160，约63秒。
处理：板端PageFlow -> audit -> PP-OCRv5 mobile -> MiniCPM3正文分段及受约束校正 -> 文字PDF与可搜索原图PDF。
V5使用板端CPU，MiniCPM3也在板端CPU运行。正文未使用聊天转录或本机目视校订文字。

选帧/全部：10张原始选帧的完整帧与裁剪图。
选帧/最终：全片去重后用于PDF的9张图，按PDF顺序命名。
PDF：两份9页PDF，分别为可编辑提取的纯文字重排版与保留原图的可搜索版。
文本：正文纯文本。
运行记录：输入哈希、录制配置、原始OCR、MiniCPM3请求响应与处理方案、PageFlow/audit记录、代码和校验用渲染。

复核说明
结尾翻回开头同一页，原p1与p10被板端V5全文相似度去重，最终保留p1。
有两张选帧的原书页码未识别，成品按选帧编号标识，没有推定或补写页码。
原p7有手指覆盖正文，部分画面页边或页码接近/超出取景范围；现有影像无法恢复未拍到或遮住的文字。
audit仍保留REVIEW：12个运动事件vs9个选帧边界，并标记p2/p4附近运动和结尾运动。运动事件也可能包含手部动作，未把计数差异强行标为PASS。
PDF已完成板端文本提取校验和页面渲染，但这不代表OCR字符准确率100%。

原始大视频仍保存在板子上，本包按要求传回选帧、两种PDF及配套记录，不重复收录大视频。
''')
font=ImageFont.truetype('/home/radxa/Project/pp-ocr-v4/simfang.ttf',23)
sheet=Image.new('RGB',(1440,3*730),'#eeeeee');draw=ImageDraw.Draw(sheet)
for k,(order,book) in enumerate(zip(selection['selected_orders'],selection['book_pages'])):
    p=by_order[order];im=Image.open(frames/p['crop_image']);im.thumbnail((465,670));x=k%3*480;y=k//3*730
    sheet.paste(im,(x+(480-im.width)//2,y+45))
    label=f'PDF{k+1} 书页{book}' if book is not None else f'PDF{k+1} 页码未识别'
    draw.text((x+12,y+12),label,fill='black',font=font)
sheet.save(root/'选帧总览.jpg',quality=92)
files={str(p.relative_to(root)):{'size':p.stat().st_size,'sha256':hashlib.file_digest(p.open('rb'),'sha256').hexdigest()}
    for p in root.rglob('*') if p.is_file() and p.name!='SHA256.json'}
(root/'SHA256.json').write_text(json.dumps(files,ensure_ascii=False,indent=2))
archive=root.with_suffix('.zip')
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
    for p in root.rglob('*'):
        if p.is_file():z.write(p,p.relative_to(root.parent))
with zipfile.ZipFile(archive) as z:assert z.testzip() is None
print(json.dumps({'archive':str(archive),'size':archive.stat().st_size,'sha256':hashlib.file_digest(archive.open('rb'),'sha256').hexdigest(),
    'selected_frames':10,'unique_pdf_pages':9,'pdfs':pdfs},ensure_ascii=False))
