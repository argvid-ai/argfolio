"""Run on the board: replace the clipped page-86 frame, retaining the prior run."""
import datetime
import json
import shutil
import subprocess
from pathlib import Path

base = Path('/home/radxa/tu/workspace/rec_20261107_164833_single_page_v5')
root = base.with_name(base.name + '_reselected86')
if root.exists():
    raise SystemExit(f'Refusing to overwrite {root}')
root.mkdir()
for name in ['frames_workspace', 'results']:
    shutil.copytree(base / name, root / name)
for name in ['input.json', 'version.json', 'pageflow_single_page.json']:
    if (base / name).exists():
        shutil.copy2(base / name, root / name)

frames = root / 'frames_workspace'
out = root / 'results'
history = root / 'previous_selection'
history.mkdir()
manifest_path = frames / 'manifest.json'
shutil.copy2(manifest_path, history / 'manifest.json')
manifest = json.loads(manifest_path.read_text())
page = next(p for p in manifest['pages'] if p['order'] == 3)
old = dict(page)
candidate = base / 'frame_review_86_90/p86_f000510.jpg'
assert candidate.is_file()
for kind in ['full', 'crop']:
    shutil.copy2(frames / page[kind + '_image'], history / (kind + '_f000462.jpg'))
    (frames / page[kind + '_image']).unlink()
    relative = f'frames/{kind}/f000510.jpg'
    shutil.copy2(candidate, frames / relative)
    page[kind + '_image'] = relative
page.update(frame_index=510, timestamp=17.0, sharpness=None, motion=None,
            mean_diff_from_previous=None, changed_fraction_from_previous=None,
            reason='review_reselection_complete_body', source='source_video_review',
            review=True, review_reasons=['Body is complete; printed footer is near image edge.'])
manifest['review_reselection'] = {
    'created_at': datetime.datetime.now().astimezone().isoformat(),
    'old': old, 'new': dict(page),
    'reason': 'Original frame clipped leftmost body characters; new frame includes complete body lines.',
    'page90': 'Retain frame 1362: reviewed alternatives did not resolve both top-line occlusion and completeness.',
    'quality_note': 'Selection improves completeness, not a claim of higher sharpness or measured OCR accuracy.'}
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
(root / 'reselection_review.json').write_text(json.dumps(manifest['review_reselection'], ensure_ascii=False, indent=2))
for filename, key in [('ocr_v5.jsonl', 'order'), ('body_minicpm3.jsonl', 'source_order')]:
    path = out / filename
    shutil.copy2(path, history / filename)
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    records = [r for r in records if r[key] != 3]
    for record in records:
        if 'image' in record:
            record['image'] = record['image'].replace(str(base), str(root))
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records))
for path in (out / 'llm_raw').glob('p003*'):
    shutil.move(str(path), str(history / path.name))
for path in out.glob('*.pdf'):
    path.unlink()  # Old PDFs remain intact in the original run and delivery.
for dirname in ['render_text', 'render_search']:
    if (out / dirname).exists():
        shutil.rmtree(out / dirname)
audit = frames / 'audit.json'
if audit.exists():
    shutil.move(str(audit), str(history / 'audit.json'))
subprocess.run(['/home/radxa/Project/pp-ocr-v4/.venv/bin/python', '/home/radxa/tu/scripts/audit.py',
                '--video', manifest['video'], '--manifest', str(manifest_path), '-o', str(audit)], check=True)
subprocess.run(['/home/radxa/tu/venv_v5/bin/python', '/home/radxa/tu/scripts/v5/board_pipeline_v5.py',
                '--source', str(frames), '--output', str(out),
                '--label', 'rec_20261107_164833_reselected86'], check=True)
print('RESELECTED_RUN', root, flush=True)
