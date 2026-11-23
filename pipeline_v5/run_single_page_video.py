"""Board entry point for a native portrait single-page recording."""
import argparse, json, subprocess, sys, hashlib
from pathlib import Path

ap=argparse.ArgumentParser()
ap.add_argument('video',type=Path)
ap.add_argument('--output',type=Path)
args=ap.parse_args()
video=args.video.expanduser().resolve()
if not video.is_file():raise SystemExit('Input video does not exist')
probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
    '-show_entries','stream=width,height','-of','json',str(video)],text=True))['streams'][0]
w,h=probe['width'],probe['height']
if w>h:
    raise SystemExit('This version accepts portrait single-page recordings only. Select 居中单页 in the capture UI. Full-frame/dual-page input requires a separate profile.')
root=(args.output or Path.home()/'tu/workspace'/f'{video.stem}_single_page_v5').expanduser().resolve()
root.mkdir(parents=True,exist_ok=True)
def app_status(stage):
    temp=root/'app_status.tmp'
    temp.write_text(json.dumps({'stage':stage},ensure_ascii=False))
    temp.replace(root/'app_status.json')
config={'roi':{'x':0,'y':0,'width':w,'height':h},'sample_fps':5.0,'motion_threshold':.025,
        'motion_confirm_frames':2,'stable_seconds':.8,'end_stable_seconds':.4,
        'detect_width':192,'blur_ksize':5,'block_rows':6,'block_cols':8,
        'dedup_mean_diff':.02,'dedup_block_diff':.05,'dedup_changed_fraction':.05,
        'min_sharpness_ratio':.5,'jpeg_quality':95}
cfg=root/'pageflow_single_page.json';cfg.write_text(json.dumps(config,ensure_ascii=False,indent=2))
frames=root/'frames_workspace'
app_status('pageflow')
subprocess.run(['python3',str(Path.home()/'Project/OCR/PageFlow/run_pageflow.py'),'analyze',
                '--video',str(video),'--output',str(frames),'--config',str(cfg)],check=True)
app_status('audit')
subprocess.run([str(Path.home()/'Project/pp-ocr-v4/.venv/bin/python'),str(Path.home()/'tu/scripts/audit.py'),
                '--video',str(video),'--manifest',str(frames/'manifest.json'),'-o',str(frames/'audit.json')],check=True)
input_info={'processing_page_mode':'single_page','video':str(video),'width':w,'height':h,
    'black_padding_added':False,'source_type':'board_native_camera_recording',
    'sha256':hashlib.file_digest(video.open('rb'),'sha256').hexdigest()}
capture_metadata=Path(str(video)+'.capture.json')
if capture_metadata.exists():input_info['capture_metadata']=json.loads(capture_metadata.read_text())
version=Path(__file__).with_name('version.json')
if version.exists():
    cfg_version=json.loads(version.read_text())
    cfg_version.pop('current_comparison_input',None)
    cfg_version['current_input']=input_info
    (root/'version.json').write_text(json.dumps(cfg_version,ensure_ascii=False,indent=2))
(root/'input.json').write_text(json.dumps(input_info,ensure_ascii=False,indent=2))
app_status('models')
subprocess.run([str(Path.home()/'tu/venv_v5/bin/python'),str(Path(__file__).with_name('board_pipeline_v5.py')),
                '--source',str(frames),'--output',str(root/'results'),'--label',video.stem],check=True)
app_status('complete')
print(root/'results')
