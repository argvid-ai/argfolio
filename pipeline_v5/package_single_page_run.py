"""Package a completed board run with final frames and model provenance."""
import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('delivery', type=Path)
    args = parser.parse_args()
    run, root = args.run, args.delivery
    frames, results = run / 'frames_workspace', run / 'results'
    manifest = json.loads((frames / 'manifest.json').read_text())
    selection = json.loads((results / 'selection.json').read_text())
    metadata = json.loads((results / 'run_manifest.json').read_text())
    assert json.loads((results / 'status.json').read_text())['stage'] == 'complete'
    assert len(selection['selected_orders']) == metadata['exports']['pages']
    assert metadata['all_body_pages_processed_by_model']
    root.mkdir(parents=True, exist_ok=False)

    def copy(source, relative):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)

    pages = {page['order']: page for page in manifest['pages']}
    for index, (order, book) in enumerate(zip(selection['selected_orders'], selection['book_pages']), 1):
        page = pages[order]
        label = f'书页{book}' if book is not None else '页码未识别'
        copy(frames / page['crop_image'], f'选帧/pdf{index:02d}_{label}_f{page["frame_index"]:06d}.jpg')
    copy(frames / 'frames' / 'crop', 'PageFlow选帧')
    for key in ('text_pdf', 'searchable_pdf'):
        pdf = Path(metadata['exports'][key])
        copy(pdf, f'PDF/{pdf.name}')
    for path in results.iterdir():
        if path.suffix != '.pdf':
            copy(path, f'运行记录/{path.name}')
    for name in ('manifest.json', 'metrics.csv', 'audit.json'):
        copy(frames / name, f'运行记录/PageFlow_audit/{name}')
    for name in ('input.json', 'version.json', 'pageflow_single_page.json', 'pipeline.log'):
        copy(run / name, f'运行记录/{name}')
    model = Path(metadata['recognition_model'])
    copy(model.with_suffix('.validation.json'), '运行记录/模型验证.json')
    for name in ('board_pipeline_v5.py', 'run_single_page_video.py'):
        copy(Path.home() / 'tu/paperscan/pipeline' / name, f'运行记录/代码/{name}')

    files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(root.rglob('*')) if path.is_file()}
    (root / 'SHA256.json').write_text(json.dumps(files, ensure_ascii=False, indent=2) + '\n')
    archive = root.with_suffix('.zip')
    temp = archive.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for path in sorted(root.rglob('*')):
            if path.is_file():
                z.write(path, path.relative_to(root.parent))
    with zipfile.ZipFile(temp) as z:
        assert z.testzip() is None
    temp.replace(archive)
    print(json.dumps({'archive': str(archive), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                      'pages': len(selection['selected_orders']), 'book_pages': selection['book_pages'],
                      'pageflow_frames': len(pages), 'files': len(files)}))


if __name__ == '__main__':
    main()
