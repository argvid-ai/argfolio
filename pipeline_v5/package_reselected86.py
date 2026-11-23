"""Package reviewed outputs on the board, without modifying the first delivery."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from pypdf import PdfReader

base = Path('/home/radxa/tu/workspace/rec_20261107_164833_single_page_v5')
run = base.with_name(base.name + '_reselected86')
res = run / 'results'
frames = run / 'frames_workspace'
root = Path('/home/radxa/tu/deliveries/Phecda_Run_20261107_164833_Reselected86')
assert not root.exists()
meta = json.loads((res / 'run_manifest.json').read_text())
selection = json.loads((res / 'selection.json').read_text())
manifest = json.loads((frames / 'manifest.json').read_text())
assert json.loads((res / 'status.json').read_text())['stage'] == 'complete'
assert len(selection['selected_orders']) == meta['body_pages_processed'] == 9
assert meta['all_body_pages_processed_by_model']
root.mkdir()

def copy(src, relative):
    dst = root / relative
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__'))
    else:
        shutil.copy2(src, dst)

by_order = {p['order']: p for p in manifest['pages']}
for i, (order, book) in enumerate(zip(selection['selected_orders'], selection['book_pages']), 1):
    page = by_order[order]
    label = f'书页{book}' if book is not None else '页码未识别'
    if order == 3:
        label = '书页86_用户定位'
    copy(frames / page['crop_image'], Path('选帧') / f'pdf{i:02d}_{label}_f{page["frame_index"]:06d}.jpg')
for key in ['text_pdf', 'searchable_pdf']:
    path = Path(meta['exports'][key])
    assert len(PdfReader(path).pages) == 9
    copy(path, Path('PDF') / path.name)
for path in res.iterdir():
    if path.is_file() and path.suffix in ['.json', '.jsonl', '.txt']:
        copy(path, Path('运行记录') / path.name)
for name in ['llm_raw', 'render_text', 'render_search']:
    copy(res / name, Path('运行记录') / name)
for name in ['manifest.json', 'audit.json', 'metrics.csv']:
    copy(frames / name, Path('运行记录/PageFlow_audit') / name)
for name in ['input.json', 'version.json', 'pageflow_single_page.json', 'reselection_review.json']:
    copy(run / name, Path('运行记录') / name)
copy(run / 'previous_selection', Path('复核/旧选帧及旧记录'))
for name in ['v5_candidate_comparison_full.json', 'timeline_p86.jpg', 'timeline_p90.jpg', 'p90_dense_best.jpg']:
    path = base / 'frame_review_86_90' / name
    if path.exists():
        copy(path, Path('复核') / name)
copy(Path('/home/radxa/tu/scripts/v5/board_pipeline_v5.py'), Path('运行记录/code/board_pipeline_v5.py'))
(root / '复核说明.txt').write_text('''选帧复核更新：第86页

第86页：将15.4秒f000462替换为17.0秒f000510。原帧多行行首在画外，新帧正文更完整；不宣称锐度或字符准确率提高。页码仍未被V5识别，PDF未补写推测页码。
第90页：保留45.4秒f001362。已查看候选时段，暂未发现同时完整、清楚且无遮挡的替代帧。原图前两行手指遮挡仍存在，未让模型编造遮住的字。

第86页的V5 OCR与MiniCPM3正文结构重新在板端运行。其余8页复用上轮板端模型结果。两份PDF均在板端重新生成，仍为9页。
V5与MiniCPM3使用板端CPU。未使用聊天转录正文或本机OCR。
原始交付保持不变；本包为另存的修订结果。audit仍为REVIEW，完整记录见运行记录。
''')
checksums = {str(p.relative_to(root)): {'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in root.rglob('*') if p.is_file()}
(root / 'SHA256.json').write_text(json.dumps(checksums, ensure_ascii=False, indent=2))
archive = root.with_suffix('.zip')
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
    for path in root.rglob('*'):
        if path.is_file():
            z.write(path, path.relative_to(root.parent))
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
print(json.dumps({'archive': str(archive), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                  'size': archive.stat().st_size}, ensure_ascii=False))
