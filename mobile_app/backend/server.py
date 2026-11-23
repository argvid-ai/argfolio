#!/usr/bin/env python3
"""PaperScan board gateway. Standard library only; models remain on the board."""
import argparse
import datetime as dt
import hashlib
import http.server
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VIDEO = re.compile(r"rec_\d{8}_\d{6}(?:_\d+)?\.mp4\Z")
ACTIVE = {"queued", "running", "cancelling"}
VERSION = '1.3.0'
PREVIEW_FPS = 20
PREVIEW_WIDTH = 640


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, value):
    temp = Path(str(path) + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


class Gateway:
    def __init__(self, data, recordings, runner, python, camera, model, token, enable_capture=True, results_roots=()):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.recordings = Path(recordings).resolve()
        self.runner, self.python = str(runner), str(python)
        self.camera, self.model, self.token = camera.rstrip('/'), model.rstrip('/'), token
        self.lock = threading.RLock()
        self.jobs = {}
        self.process = None
        self.stopping = False
        self.enable_capture = enable_capture
        self.results_roots = [Path(p).resolve() for p in results_roots]
        self.scan_lock = threading.Lock()
        self.stop_scan = threading.Event()
        self.deleted_imports = set(read_json(self.data / 'deleted-imports.json', []))
        for path in self.data.glob('*/job.json'):
            job = read_json(path)
            if not job or job.get('id') != path.parent.name:
                continue
            if job.get('state') in ACTIVE:
                job.update(state='interrupted', error='服务重新启动，点击重试可恢复处理。', updated_at=now())
                write_json(path, job)
            self.jobs[job['id']] = job

    def request_camera(self, path, payload=None):
        raw = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.camera + path, data=raw,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=45 if path == '/record/stop' else 6) as r:
            return json.load(r)

    def preview_frames(self):
        from PIL import Image

        state = self.request_camera('/status')
        source_width, source_height = state['width'], state['height']
        if not source_width or not source_height:
            raise OSError('Camera has no frames')
        x, y, width, height = state['crop']
        if state['capture_mode'] != 'single_center':
            width = min(1536, source_width)
            x, y, height = (source_width - width) // 2, 0, source_height
        output_size = (min(width, PREVIEW_WIDTH), round(height * min(width, PREVIEW_WIDTH) / width))
        with urllib.request.urlopen(self.camera + '/stream', timeout=4) as source:
            next_frame = 0.0
            while True:
                line = source.readline(512)
                if not line:
                    return
                if line.strip() != b'--FRAME':
                    continue
                size = None
                for _ in range(12):
                    header = source.readline(512)
                    if header == b'\r\n':
                        break
                    if header.lower().startswith(b'content-length:'):
                        size = int(header.split(b':', 1)[1])
                if not size or size > 12 * 1024 * 1024:
                    raise OSError('Invalid camera frame length')
                jpeg = source.read(size)
                now = time.monotonic()
                if now < next_frame - 0.002:
                    continue
                next_frame = max(next_frame + 1 / PREVIEW_FPS, now + 1 / PREVIEW_FPS if not next_frame else now)
                with Image.open(io.BytesIO(jpeg)) as frame:
                    frame.draft('RGB', (frame.width // 2, frame.height // 2))
                    sx, sy = frame.width / source_width, frame.height / source_height
                    crop = frame.crop(tuple(round(value) for value in (x*sx, y*sy, (x+width)*sx, (y+height)*sy)))
                    preview = crop.resize(output_size, Image.Resampling.BILINEAR)
                    buffer = io.BytesIO()
                    preview.save(buffer, 'JPEG', quality=76)
                    yield buffer.getvalue()

    def active(self):
        return next((j for j in self.jobs.values() if j['state'] in ACTIVE), None)

    def status(self):
        with self.lock:
            active = self.active()
            base = dict(connected=True, active_job=active['id'] if active else None,
                        capture_enabled=self.enable_capture, document_count=len(self.jobs),
                        version=VERSION, page_mode='single_page', ocr_backend='board_cpu')
        if not self.enable_capture:
            return base
        state, error = None, None
        try:
            state = self.request_camera('/status')
        except (OSError, ValueError) as exc:
            error = '摄像头服务未连接，请检查板子和摄像头服务。'
        model_ok = False
        try:
            with urllib.request.urlopen(self.model + '/health', timeout=2) as r:
                model_ok = r.status == 200
        except OSError:
            pass
        return dict(base, camera=state, camera_error=error, model_ready=model_ok,
                    pipeline_ready=Path(self.runner).is_file() and Path(self.python).is_file(),
                    preview_fps=PREVIEW_FPS,
                    )

    def save(self, job):
        write_json(self.data / job['id'] / 'job.json', job)

    def public_job(self, job):
        result = dict(job)
        work = self.work_dir(job)
        if job['state'] in {'running', 'failed', 'interrupted'}:
            progress = read_json(work / 'app_status.json', {})
            model_progress = read_json(work / 'results' / 'status.json', {})
            if progress.get('stage') in ('models', 'complete') and model_progress:
                progress = model_progress
            if progress.get('stage') == 'complete' and job['state'] == 'running':
                progress = {'stage': 'export'}
            result['progress'] = progress
        result.pop('internal_video', None)
        result.pop('source_signature', None)
        result.pop('source_manifest', None)
        result['can_retry'] = (
            self.enable_capture and job['state'] in {'failed', 'interrupted', 'cancelled'}
            and bool(VIDEO.fullmatch(job['name'])) and (self.recordings / job['name']).is_file()
        )
        return result

    def work_dir(self, job):
        attempt = job.get('attempt', 1)
        return self.data / job['id'] / ('work' if attempt == 1 else f'work_{attempt}')

    def start(self, name, retry=None):
        with self.lock:
            if not self.enable_capture:
                raise ValueError('当前为结果查看模式。')
            if self.stopping:
                raise ValueError('服务正在关闭。')
            if self.active():
                raise ValueError('板子正在处理另一个任务，请等待完成。')
            if not VIDEO.fullmatch(name or ''):
                raise ValueError('请选择有效的单页录制文件。')
            video = (self.recordings / name).resolve()
            if video.parent != self.recordings or not video.is_file():
                raise ValueError('录制文件不存在。')
            try:
                state = self.request_camera('/status')
            except OSError:
                state = {}
            if state.get('recording'):
                raise ValueError('请先停止录制。')
            if not Path(self.runner).is_file() or not Path(self.python).is_file():
                raise ValueError('板端处理程序未安装，请先完成部署。')
            if retry:
                job = self.jobs.get(retry)
                if not job or not self.public_job(job)['can_retry']:
                    raise ValueError('这个任务不能重试。')
                job['attempt'] = job.get('attempt', 1) + 1
            else:
                job = dict(id=uuid.uuid4().hex, name=name, created_at=now(), sample=False)
                (self.data / job['id']).mkdir()
                self.jobs[job['id']] = job
            job.update(state='queued', error=None, updated_at=now(), downloads=[])
            self.save(job)
            threading.Thread(target=self.run_job, args=(job, video), daemon=True).start()
            return self.public_job(job)

    def run_job(self, job, video):
        proc = None
        try:
            work = self.work_dir(job)
            with (self.data / job['id'] / 'process.log').open('ab') as log:
                with self.lock:
                    if job['state'] == 'cancelling':
                        return
                    if self.stopping:
                        raise RuntimeError('服务正在关闭')
                    job.update(state='running', updated_at=now())
                    self.save(job)
                    self.process = subprocess.Popen([self.python, '-u', self.runner, str(video),
                        '--output', str(work)], stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True)
                    proc = self.process
                code = proc.wait()
            with self.lock:
                if job['state'] == 'cancelling':
                    return
            if code:
                model_status = read_json(work / 'results' / 'status.json', {})
                if model_status.get('stage') == 'no_text':
                    raise RuntimeError('选帧没有识别到可用的正文。请把书页放入取景框，保持清晰后重新录制。')
                raise RuntimeError('板端处理失败，错误码 %s。请下载处理日志后检查或重试。' % code)
            results = work / 'results'
            manifest = read_json(results / 'run_manifest.json')
            if not manifest or not manifest.get('all_body_pages_processed_by_model'):
                raise RuntimeError('缺少完整的板端模型处理记录，未发布 PDF。')
            downloads = self.collect(job['id'], results, manifest, work / 'frames_workspace' / 'audit.json')
            previews = self.collect_previews(job['id'], results)
            text_previews = self.collect_previews(job['id'], results, variant='text')
            with self.lock:
                if job['state'] == 'cancelling':
                    return
                job.update(state='complete', updated_at=now(), downloads=downloads,
                           pages=manifest['exports']['pages'], review=self.audit_state(work),
                           preview_pages=previews, text_preview_pages=text_previews)
                self.save(job)
        except Exception as exc:
            with self.lock:
                if job['state'] != 'cancelling':
                    job.update(state='interrupted' if self.stopping else 'failed', error=str(exc), updated_at=now())
                    self.save(job)
        finally:
            with self.lock:
                if self.process is proc:
                    self.process = None
                if job['state'] == 'cancelling':
                    if proc is not None:
                        self.signal_group(proc, signal.SIGKILL)
                    for path in ('downloads', 'previews'):
                        shutil.rmtree(self.data / job['id'] / path, ignore_errors=True)
                    job.update(state='cancelled', updated_at=now(), downloads=[])
                    self.save(job)

    @staticmethod
    def signal_group(proc, sig):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if not job or job['state'] not in ACTIVE | {'cancelled'}:
            raise ValueError('该任务已结束或不存在。')
        if job['state'] in {'cancelling', 'cancelled'}:
            return self.public_job(job)
        job.update(state='cancelling', updated_at=now())
        self.save(job)
        proc = self.process
        if proc is not None and proc.poll() is None:
            self.signal_group(proc, signal.SIGTERM)

            def finish_cancel():
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    with self.lock:
                        if self.process is proc and job['state'] == 'cancelling':
                            self.signal_group(proc, signal.SIGKILL)

            threading.Thread(target=finish_cancel, daemon=True).start()
        return self.public_job(job)

    def delete_job(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return
        if job['state'] in ACTIVE:
            raise ValueError('请先中断处理，等任务结束后再删除。')
        if job.get('imported'):
            deleted = self.deleted_imports | {job_id}
            write_json(self.data / 'deleted-imports.json', sorted(deleted))
            self.deleted_imports = deleted
        folder = self.data / job_id
        trash = self.data / '.trash'
        trash.mkdir(exist_ok=True)
        folder.rename(trash / f'{job_id}-{uuid.uuid4().hex}')
        del self.jobs[job_id]

    def delete_recording(self, name):
        if not isinstance(name, str) or not VIDEO.fullmatch(name):
            raise ValueError('录制文件名无效。')
        if self.active() and self.active()['name'] == name:
            raise ValueError('请先中断这段视频的处理。')
        if self.request_camera('/status').get('recording'):
            raise ValueError('请先停止录制。')
        file = self.recordings / name
        if file.is_symlink() or not file.is_file():
            raise ValueError('录制文件不存在。')
        trash = self.recordings / '.trash' / uuid.uuid4().hex
        trash.mkdir(parents=True)
        metadata = file.with_suffix(file.suffix + '.capture.json')
        if metadata.is_file() and not metadata.is_symlink():
            metadata.rename(trash / metadata.name)
        try:
            file.rename(trash / file.name)
        except OSError:
            backup = trash / metadata.name
            if backup.exists():
                backup.rename(metadata)
            trash.rmdir()
            raise

    def audit_state(self, work):
        audit = read_json(work / 'frames_workspace' / 'audit.json', {})
        return str(audit.get('verdict', audit.get('status', 'REVIEW')))

    def collect(self, job_id, results, manifest, audit=None):
        exports = manifest.get('exports', {})
        if not exports.get('pages') or not exports.get('text_roundtrip_exact') or not exports.get('both_rendered'):
            raise RuntimeError('PDF 导出校验未通过。')
        target = self.data / job_id / 'downloads'
        target.mkdir(exist_ok=True)
        items = []
        for key, label in [('text_pdf', '文字版 PDF'), ('searchable_pdf', '可搜索原图 PDF')]:
            # Old manifests contain board absolute paths. Only use names inside this job's results.
            file = results / Path(exports[key]).name
            if not file.is_file() or file.is_symlink():
                raise RuntimeError('PDF 文件缺失或无效。')
            with file.open('rb') as stream:
                if stream.read(5) != b'%PDF-':
                    raise RuntimeError('PDF 文件无效。')
            dest = target / file.name
            temporary = target / ('.' + file.name + '.tmp')
            shutil.copy2(file, temporary)
            temporary.replace(dest)
            items.append(dict(name=dest.name, label=label, size=dest.stat().st_size,
                              sha256=hashlib.sha256(dest.read_bytes()).hexdigest()))
        for file in results.glob('*.txt'):
            if not file.is_symlink():
                shutil.copy2(file, target / file.name)
        write_json(target / 'run_manifest.json', manifest)
        if audit and audit.is_file():
            shutil.copy2(audit, target / 'audit.json')
        frames = self.selected_frames(results, audit)
        archive = target / 'PDF与处理记录.zip'
        temporary = archive.with_suffix('.zip.tmp')
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for file in sorted(target.iterdir()):
                if file not in (archive, temporary) and file.is_file():
                    z.write(file, file.name)
            for number, frame in enumerate(frames, 1):
                z.write(frame, f'选帧/{number:04d}_{frame.name}')
        temporary.replace(archive)
        items.append(dict(name=archive.name, label='完整结果包' if frames else 'PDF 与处理记录', size=archive.stat().st_size,
                          sha256=hashlib.sha256(archive.read_bytes()).hexdigest()))
        return items

    def selected_frames(self, results, audit):
        if not audit:
            return []
        workspace = audit.parent.resolve()
        manifest = read_json(workspace / 'manifest.json', {})
        selection = read_json(results / 'selection.json', {})
        pages = {page['order']: page for page in manifest.get('pages', [])}
        frames = []
        for order in selection.get('selected_orders', []):
            path = (workspace / pages[order]['crop_image']).resolve()
            if not path.is_relative_to(workspace) or not path.is_file():
                raise RuntimeError('选帧文件缺失或路径无效。')
            frames.append(path)
        return frames

    def collect_previews(self, job_id, results, variant='search'):
        """Publish already rendered page images; PDF rendering stays on the board."""
        source = results / f'render_{variant}'
        files = sorted(source.glob('page-*.png'), key=lambda p: int(p.stem.split('-')[-1])) if source.is_dir() else []
        if not files:
            return 0
        target = self.data / job_id / 'previews'
        if variant == 'text':
            target /= 'text'
        target.mkdir(parents=True, exist_ok=True)
        count = 0
        for file in files:
            if file.is_symlink() or not file.is_file():
                return 0
            with file.open('rb') as f:
                if f.read(8) != b'\x89PNG\r\n\x1a\n':
                    return 0
            count += 1
            temp = target / f'{count:04d}.tmp'
            shutil.copy2(file, temp)
            temp.replace(target / f'{count:04d}.png')
        return count

    def scan_results(self):
        """Index completed board exports without camera/model services."""
        if not self.scan_lock.acquire(blocking=False):
            return
        try:
            for root in self.results_roots:
                manifests = {root / 'run_manifest.json'}
                manifests.update(root.glob('*/run_manifest.json'))
                manifests.update(root.glob('*/results/run_manifest.json'))
                for path in sorted(manifests):
                    try:
                        resolved = path.resolve()
                        if not path.is_file() or path.is_symlink() or not resolved.is_relative_to(root):
                            continue
                        manifest = read_json(path, {})
                        exports = manifest.get('exports', {})
                        if not manifest.get('all_body_pages_processed_by_model') or not exports.get('text_roundtrip_exact') or not exports.get('both_rendered'):
                            continue
                        results = path.parent
                        # A run manifest may remain from a previous run while files are being regenerated.
                        state = read_json(results / 'status.json', {})
                        if state and state.get('stage') != 'complete':
                            continue
                        files = [path] + [results / Path(exports[key]).name for key in ('text_pdf', 'searchable_pdf')]
                        if any(f.is_symlink() or not f.is_file() for f in files):
                            continue
                        signatures = [(str(f), f.stat().st_size, f.stat().st_mtime_ns) for f in files]
                        signature = hashlib.sha256(json.dumps({'schema': 3, 'files': signatures}).encode()).hexdigest()
                        job_id = 'board_' + hashlib.sha256(str(resolved).encode()).hexdigest()[:20]
                        with self.lock:
                            if job_id in self.deleted_imports:
                                continue
                            previous = self.jobs.get(job_id, {})
                            if previous.get('source_signature') == signature:
                                continue
                        (self.data / job_id).mkdir(exist_ok=True)
                        audit = results.parent / 'frames_workspace' / 'audit.json'
                        if not audit.is_file():
                            audit = results / 'audit.json'
                        downloads = self.collect(job_id, results, manifest, audit)
                        previews = self.collect_previews(job_id, results)
                        text_previews = self.collect_previews(job_id, results, variant='text')
                        if signatures != [(str(f), f.stat().st_size, f.stat().st_mtime_ns) for f in files]:
                            continue
                        audit_record = read_json(audit, {})
                        review = str(audit_record.get('verdict', audit_record.get('status', 'REVIEW')))
                        title = manifest.get('output_label') or Path(manifest.get('source_video') or '').stem or f'板端文档 · {exports_pages(manifest)} 页'
                        job = dict(id=job_id, name=title,
                            created_at=manifest.get('generated_at') or now(), updated_at=now(),
                            state='complete', sample=False, imported=True, downloads=downloads,
                            pages=exports_pages(manifest), preview_pages=previews,
                            text_preview_pages=text_previews, review=review,
                            source_manifest=str(resolved), source_signature=signature)
                        if 'reselected86' in str(path):
                            job['name'] = '单页扫描 · 第 86 页修订版'
                            job['note'] = '第 86 页已换帧；第 90 页前两行手指遮挡仍需复核。'
                        with self.lock:
                            if job_id in self.deleted_imports:
                                shutil.rmtree(self.data / job_id, ignore_errors=True)
                                continue
                            self.jobs[job_id] = job
                            self.save(job)
                    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
                        continue
        finally:
            self.scan_lock.release()

    def watch_results(self):
        while not self.stop_scan.is_set():
            self.scan_results()
            self.stop_scan.wait(15)

    def import_sample(self, source):
        source = Path(source).resolve()
        manifest = read_json(source / '运行记录' / 'run_manifest.json')
        if not manifest:
            raise ValueError('找不到样例运行记录。')
        job_id = 'sample_20261107'
        folder = self.data / job_id
        folder.mkdir(exist_ok=True)
        items = self.collect(job_id, source / 'PDF', manifest,
                             source / '运行记录' / 'PageFlow_audit' / 'audit.json')
        job = dict(id=job_id, name='昨天的 9 页修订结果', created_at=now(), updated_at=now(),
                   state='complete', sample=True, downloads=items, pages=exports_pages(manifest),
                   review='REVIEW', note='第 86 页已换帧；第 90 页前两行手指遮挡仍需复核。')
        self.jobs[job_id] = job
        self.save(job)

    def shutdown(self):
        self.stop_scan.set()
        with self.lock:
            self.stopping = True
            proc = self.process
        if proc and proc.poll() is None:
            self.signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.signal_group(proc, signal.SIGKILL)


def exports_pages(manifest):
    return manifest.get('exports', {}).get('pages', 0)


def handler_for(gateway, local_access=False):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not put pairing tokens in access logs.

        def send_bytes(self, data, ctype, code=200, disposition=None):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            if disposition:
                self.send_header('Content-Disposition', disposition)
            self.end_headers()
            self.wfile.write(data)

        def json(self, value, code=200):
            self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8', code)

        def preview_stream(self):
            from contextlib import closing

            with closing(gateway.preview_frames()) as frames:
                try:
                    first = next(frames)
                except (OSError, ValueError, ImportError, StopIteration):
                    return self.json({'error': '摄像头预览暂不可用。'}, 503)
                self.send_response(200)
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=PREVIEW')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                self.connection.settimeout(5)
                try:
                    from itertools import chain
                    for jpeg in chain((first,), frames):
                        self.wfile.write(b'--PREVIEW\r\nContent-Type: image/jpeg\r\n'
                                         + b'Content-Length: %d\r\n\r\n' % len(jpeg) + jpeg + b'\r\n')
                except (OSError, ValueError):
                    pass

        def authorized(self):
            if local_access:
                host = self.headers.get('Host', '')
                if not re.fullmatch(r'(127\.0\.0\.1|localhost)(:\d+)?', host):
                    self.json({'error': '请通过本机工作台地址访问。'}, 403)
                    return False
                if self.command == 'POST' and (
                    self.headers.get('X-PaperScan') != 'workbench'
                    or self.headers.get('Origin') not in (None, 'http://' + host)
                ):
                    self.json({'error': '请在工作台页面中操作。'}, 403)
                    return False
                return True
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            token = self.headers.get('Authorization', '').removeprefix('Bearer ')
            token = token or query.get('token', [''])[0]
            if not secrets.compare_digest(token, gateway.token):
                self.json({'error': '配对码错误，请重新连接。'}, 401)
                return False
            return True

        def do_GET(self):
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            try:
                if path == '/health':
                    return self.json({'ok': True, 'service': 'cix-paperscan', 'version': VERSION,
                                      'local_access': local_access})
                if path in ('/', '/index.html', '/app.js', '/style.css', '/manifest.webmanifest', '/icon.svg', '/apple-touch-icon.png'):
                    file = ROOT / 'web' / ('index.html' if path == '/' else path[1:])
                    return self.send_bytes(file.read_bytes(), mimetypes.guess_type(file)[0] or 'application/octet-stream')
                if not self.authorized():
                    return
                if path == '/api/status':
                    return self.json(gateway.status())
                if path == '/api/videos':
                    if not gateway.enable_capture:
                        return self.json({'files': []})
                    return self.json(gateway.request_camera('/files'))
                if path == '/api/jobs':
                    with gateway.lock:
                        jobs = [gateway.public_job(j) for j in gateway.jobs.values()]
                    return self.json({'jobs': sorted(jobs, key=lambda x: x['created_at'], reverse=True)})
                if path == '/api/snapshot.jpg':
                    if not gateway.enable_capture:
                        return self.json({'error': '当前为结果查看模式。'}, 404)
                    with urllib.request.urlopen(gateway.camera + '/snapshot.jpg', timeout=5) as r:
                        return self.send_bytes(r.read(12 * 1024 * 1024), 'image/jpeg')
                if path == '/api/preview.mjpeg':
                    if not gateway.enable_capture:
                        return self.json({'error': '当前为结果查看模式。'}, 404)
                    return self.preview_stream()
                preview = re.fullmatch(r'/preview/([a-z0-9_]+)/(text/)?(\d{4})\.png', path)
                if preview:
                    job_id, variant, page = preview.groups()
                    job = gateway.jobs.get(job_id, {})
                    count = job.get('text_preview_pages' if variant else 'preview_pages', 0)
                    if not 1 <= int(page) <= count:
                        return self.json({'error': '页面不存在。'}, 404)
                    file = gateway.data / job_id / 'previews' / ('text' if variant else '') / f'{page}.png'
                    if file.is_symlink() or not file.is_file():
                        return self.json({'error': '页面不存在。'}, 404)
                    return self.send_bytes(file.read_bytes(), 'image/png')
                match = re.fullmatch(r'/download/([a-z0-9_]+)/([^/]+)', path)
                if match:
                    job_id, name = match.groups()
                    job = gateway.jobs.get(job_id, {})
                    if name == 'process.log':
                        file = gateway.data / job_id / name
                    elif any(d['name'] == name for d in job.get('downloads', [])):
                        file = gateway.data / job_id / 'downloads' / name
                    else:
                        return self.json({'error': '文件不存在。'}, 404)
                    if not file.is_file() or file.is_symlink():
                        return self.json({'error': '文件不存在。'}, 404)
                    disposition = "attachment; filename=download; filename*=UTF-8''" + urllib.parse.quote(name)
                    # Stream PDFs/ZIPs to avoid holding large multi-page books in RAM.
                    self.send_response(200)
                    self.send_header('Content-Type', mimetypes.guess_type(name)[0] or 'application/octet-stream')
                    self.send_header('Content-Length', str(file.stat().st_size))
                    self.send_header('Content-Disposition', disposition)
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.end_headers()
                    with file.open('rb') as f:
                        shutil.copyfileobj(f, self.wfile, 256 * 1024)
                    return
                self.json({'error': '页面不存在。'}, 404)
            except (OSError, ValueError) as exc:
                self.json({'error': '板子服务未连接或数据暂不可用。'}, 503)

        def do_POST(self):
            if not self.authorized():
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 8192:
                    return self.json({'error': '请求过大。'}, 413)
                body = json.loads(self.rfile.read(size) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('无效请求。')
                path = urllib.parse.urlsplit(self.path).path
                with gateway.lock:
                    match = re.fullmatch(r'/api/jobs/([a-z0-9_]+)/delete', path)
                    if match:
                        gateway.delete_job(match.group(1))
                        return self.json({'ok': True})
                    if not gateway.enable_capture:
                        return self.json({'error': '当前为结果查看模式。'}, 409)
                    match = re.fullmatch(r'/api/jobs/([a-z0-9_]+)/cancel', path)
                    if match:
                        return self.json(gateway.cancel(match.group(1)), 202)
                    if path == '/api/videos/delete':
                        gateway.delete_recording(body.get('name'))
                        return self.json({'ok': True})
                    if path == '/api/record/start':
                        if gateway.active():
                            raise ValueError('请等待文档处理完成后再录制。')
                        if gateway.request_camera('/status').get('recording'):
                            raise ValueError('摄像头已在录制。')
                        result = gateway.request_camera('/capture/mode', {'mode': 'single_center'})
                        if result.get('ok') is False:
                            raise ValueError(result.get('error', '无法切换单页模式。'))
                        return self.json(gateway.request_camera('/record/start', {}))
                    if path == '/api/record/stop':
                        result = gateway.request_camera('/record/stop', {})
                        if body.get('process') is True and result.get('ok') and result.get('file'):
                            try:
                                result['job'] = gateway.start(result['file'])
                            except (ValueError, OSError) as exc:
                                result['processing_error'] = str(exc)
                        return self.json(result)
                    if path == '/api/jobs':
                        return self.json(gateway.start(body.get('name')), 202)
                    match = re.fullmatch(r'/api/jobs/([a-z0-9_]+)/retry', path)
                    if match:
                        job = gateway.jobs.get(match.group(1))
                        if not job:
                            raise ValueError('任务不存在。')
                        return self.json(gateway.start(job['name'], retry=job['id']), 202)
                self.json({'error': '接口不存在。'}, 404)
            except (ValueError, KeyError, TypeError) as exc:
                self.json({'error': str(exc)}, 409)
            except urllib.error.HTTPError as exc:
                payload = json.loads(exc.read())
                return self.json({'error': payload.get('error', '摄像头操作失败。')}, exc.code)
            except OSError:
                self.json({'error': '无法连接摄像头服务，请检查板子。'}, 503)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8088)
    ap.add_argument('--data', type=Path, default=Path.home() / 'tu/paperscan/jobs')
    ap.add_argument('--token-file', type=Path, help='Reuse the existing board pairing code')
    ap.add_argument('--local-access', action='store_true', help='Access through a trusted SSH loopback tunnel')
    ap.add_argument('--paired-tls-port', type=int, help='Additional authenticated HTTPS listener on loopback')
    ap.add_argument('--tls-cert', type=Path)
    ap.add_argument('--tls-key', type=Path)
    ap.add_argument('--recordings', type=Path, default=Path.home() / 'tu/recordings/c60e_20261107')
    ap.add_argument('--runner', type=Path, default=Path.home() / 'tu/scripts/v5/run_single_page_video.py')
    ap.add_argument('--python', type=Path, default=Path.home() / 'tu/venv_v5/bin/python')
    ap.add_argument('--camera', default='http://127.0.0.1:8080')
    ap.add_argument('--model', default='http://127.0.0.1:8090')
    ap.add_argument('--import-sample', type=Path)
    ap.add_argument('--results-root', action='append', type=Path, help='Watch a board workspace for completed exports; repeatable')
    ap.add_argument('--enable-capture', action='store_true', help='Optional capture controls; default is document viewer only')
    args = ap.parse_args()
    if args.local_access and args.host != '127.0.0.1':
        ap.error('--local-access requires --host 127.0.0.1')
    if args.paired_tls_port and (not args.tls_cert or not args.tls_key):
        ap.error('--paired-tls-port requires --tls-cert and --tls-key')
    args.data.mkdir(parents=True, exist_ok=True)
    token_file = args.token_file or args.data / 'pairing-token.txt'
    if not token_file.exists():
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_urlsafe(24))
    token = token_file.read_text().strip()
    if len(token) < 16:
        raise SystemExit('Pairing token must contain at least 16 characters')
    gateway = Gateway(args.data, args.recordings, args.runner, args.python, args.camera, args.model, token,
                      enable_capture=args.enable_capture,
                      results_roots=args.results_root or [Path.home() / 'tu/workspace'])
    if args.import_sample:
        gateway.import_sample(args.import_sample)
    gateway.scan_results()
    threading.Thread(target=gateway.watch_results, daemon=True).start()
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler_for(gateway, args.local_access))
    server.daemon_threads = True
    paired = None
    if args.paired_tls_port:
        import ssl
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.tls_cert, args.tls_key)
        paired = http.server.ThreadingHTTPServer(('127.0.0.1', args.paired_tls_port), handler_for(gateway))
        paired.socket = tls.wrap_socket(paired.socket, server_side=True)
        threading.Thread(target=paired.serve_forever, daemon=True).start()
    print(f'PaperScan listening on port {args.port}. Pairing code: {token_file}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gateway.shutdown()
        server.server_close()
        if paired:
            paired.shutdown()
            paired.server_close()


if __name__ == '__main__':
    main()
