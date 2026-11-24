import http.server
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location('gateway', Path(__file__).resolve().parents[1]/'backend/server.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Camera(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        payload = {'recording':False} if self.path == '/status' else {'files':[]}
        raw = json.dumps(payload).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path != '/record/stop':
            self.send_error(404)
            return
        raw = b'{"ok":true,"file":"rec_20261108_120000.mp4"}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recordings = self.root/'recordings'; self.recordings.mkdir()
        (self.recordings/'rec_20261108_120000.mp4').write_bytes(b'test video')
        self.camera = http.server.ThreadingHTTPServer(('127.0.0.1',0),Camera)
        threading.Thread(target=self.camera.serve_forever,daemon=True).start()
        # Deterministic runner fixture exercises actual subprocess and file publishing.
        self.runner = self.root/'runner.py'
        self.runner.write_text('''import sys,json,time
from pathlib import Path
root=Path(sys.argv[sys.argv.index('--output')+1]);r=root/'results';r.mkdir(parents=True,exist_ok=True)
time.sleep(.15)
for name in ['text.pdf','search.pdf']:(r/name).write_bytes(b'%PDF-1.4\\nfixture')
(r/'run_manifest.json').write_text(json.dumps({'all_body_pages_processed_by_model':True,'exports':{'text_pdf':str(r/'text.pdf'),'searchable_pdf':str(r/'search.pdf'),'pages':2,'text_roundtrip_exact':True,'both_rendered':True}}))
''')
        self.token='fixture-pairing-token-12345'
        self.g=module.Gateway(self.root/'jobs',self.recordings,self.runner,sys.executable,
            f'http://127.0.0.1:{self.camera.server_port}','http://127.0.0.1:1',self.token)
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),module.handler_for(self.g))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.g.shutdown();self.server.shutdown();self.server.server_close();self.camera.shutdown();self.camera.server_close();self.temp.cleanup()

    def get(self,path,body=None,token=True):
        req=urllib.request.Request(self.base+path,data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+self.token} if token else {})
        try:
            with urllib.request.urlopen(req,timeout=5) as r:return r.status,r.read()
        except urllib.error.HTTPError as e:return e.code,e.read()

    def wait_job(self,job):
        for _ in range(100):
            if self.g.jobs[job['id']]['state'] not in module.ACTIVE:return self.g.jobs[job['id']]
            time.sleep(.03)
        self.fail('job timed out')

    def test_pairing_and_public_health(self):
        self.assertEqual(self.get('/health',token=False)[0],200)
        self.assertEqual(self.get('/api/jobs',token=False)[0],401)
        self.assertEqual(self.get('/api/jobs')[0],200)
        self.assertEqual(self.get('/api/jobs',body=[],token=True)[0],409)

    def test_media_cookie_scope(self):
        request = urllib.request.Request(self.base + '/api/status', headers={
            'Authorization': 'Bearer ' + self.token, 'X-PaperScan-Media': 'https',
        })
        with urllib.request.urlopen(request, timeout=5) as response:
            value = response.headers['Set-Cookie']
        for flag in ('HttpOnly', 'SameSite=Strict', 'Secure'):
            self.assertIn(flag, value)
        cookie = value.split(';', 1)[0]
        for path, method, expected in (
            ('/download/missing/missing.pdf', 'GET', 404),
            ('/api/status', 'GET', 401),
            ('/api/status', 'POST', 401),
        ):
            request = urllib.request.Request(self.base + path, method=method, headers={'Cookie': cookie})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, expected)

    def test_pipeline_download_and_path_confinement(self):
        code,raw=self.get('/api/jobs',{'name':'rec_20261108_120000.mp4'})
        self.assertEqual(code,202)
        job=self.wait_job(json.loads(raw));self.assertEqual(job['state'],'complete')
        self.assertEqual(len(job['downloads']),3)
        url=f"/download/{job['id']}/text.pdf"
        self.assertEqual(self.get(url,token=False)[0],401)
        self.assertEqual(self.get(url)[1],b'%PDF-1.4\nfixture')
        self.assertEqual(self.get(url+'?token='+self.token,token=False)[0],401)
        self.assertEqual(self.get(f"/download/{job['id']}/..%2Fjob.json")[0],404)
        self.assertEqual(self.get('/api/jobs',{'name':'../rec_20261108_120000.mp4'})[0],409)

    def test_failure_retry_and_restart_recovery(self):
        self.runner.write_text('raise SystemExit(7)')
        code,raw=self.get('/api/jobs',{'name':'rec_20261108_120000.mp4'})
        job=self.wait_job(json.loads(raw));self.assertEqual(job['state'],'failed');self.assertIn('7',job['error'])
        self.assertEqual(job['downloads'],[])
        self.assertEqual(self.get(f"/api/jobs/{job['id']}/retry",{})[0],202)
        self.wait_job(job)
        job['state']='running';self.g.save(job)
        restarted=module.Gateway(self.g.data,self.recordings,self.runner,sys.executable,self.g.camera,self.g.model,self.token)
        self.assertEqual(restarted.jobs[job['id']]['state'],'interrupted')

    def test_exclusive_job(self):
        _,raw=self.get('/api/jobs',{'name':'rec_20261108_120000.mp4'})
        self.assertEqual(self.get('/api/jobs',{'name':'rec_20261108_120000.mp4'})[0],409)
        self.assertEqual(self.get('/api/record/start',{})[0],409)
        self.wait_job(json.loads(raw))

    def test_cancel_stops_processing_and_preserves_recording_for_retry(self):
        runner = self.runner.read_text()
        self.runner.write_text('import time\ntime.sleep(60)\n')
        name = 'rec_20261108_120000.mp4'
        _, raw = self.get('/api/jobs', {'name': name})
        job = json.loads(raw)
        for _ in range(100):
            if self.g.process is not None:
                break
            time.sleep(.03)
        self.assertIsNotNone(self.g.process)
        proc = self.g.process
        route = f"/api/jobs/{job['id']}"
        self.assertEqual(self.get(route + '/delete', {})[0], 409)
        self.assertEqual(self.get('/api/videos/delete', {'name': name})[0], 409)
        self.assertEqual(self.get(route + '/cancel', {}, token=False)[0], 401)
        self.assertEqual(self.get(route + '/cancel', {})[0], 202)
        cancelled = self.wait_job(job)
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertIsNotNone(proc.poll())
        self.assertTrue(self.g.public_job(cancelled)['can_retry'])
        self.runner.write_text(runner)
        self.assertEqual(self.get(route + '/retry', {})[0], 202)
        self.assertEqual(self.wait_job(job)['state'], 'complete')

    def test_delete_recording_and_result_are_independent(self):
        name = 'rec_20261108_120000.mp4'
        _, raw = self.get('/api/jobs', {'name': name})
        job = self.wait_job(json.loads(raw))
        path = f"/download/{job['id']}/text.pdf"
        metadata = self.recordings/(name+'.capture.json')
        metadata.write_text('{"capture_mode":"single_center"}')
        self.assertEqual(self.get('/api/videos/delete', {'name': '../'+name})[0], 409)
        self.assertEqual(self.get('/api/videos/delete', {'name': name})[0], 200)
        self.assertFalse((self.recordings/name).exists())
        self.assertFalse(metadata.exists())
        self.assertEqual(len(list((self.recordings/'.trash').glob('*/*.mp4'))), 1)
        self.assertEqual(len(list((self.recordings/'.trash').glob('*/*.capture.json'))), 1)
        self.assertEqual(self.get(path)[0], 200)
        self.assertEqual(self.get(f"/api/jobs/{job['id']}/delete", {})[0], 200)
        self.assertEqual(self.get(path)[0], 404)
        self.assertEqual(len(list((self.g.data/'.trash').iterdir())), 1)

    def test_stop_and_generate_returns_a_trackable_job(self):
        code, raw = self.get('/api/record/stop', {'process': True})
        self.assertEqual(code, 200)
        result = json.loads(raw)
        self.assertEqual(result['file'], 'rec_20261108_120000.mp4')
        job = self.wait_job(result['job'])
        self.assertEqual(job['state'], 'complete')
        self.assertEqual(job['pages'], 2)
        code, raw = self.get('/api/record/stop', {})
        self.assertEqual(code, 200)
        self.assertNotIn('job', json.loads(raw))

    def test_unavailable_services_are_truthful(self):
        code,raw=self.get('/api/status');self.assertEqual(code,200)
        status=json.loads(raw);self.assertFalse(status['model_ready']);self.assertTrue(status['pipeline_ready'])

    def test_documents_work_without_camera_and_discover_new_exports(self):
        self.g.enable_capture=False
        self.g.request_camera=lambda *args: self.fail('Document viewer must not contact camera')
        code,raw=self.get('/api/status');self.assertEqual(code,200)
        self.assertFalse(json.loads(raw)['capture_enabled'])
        self.assertEqual(self.get('/api/record/start',{})[0],409)
        workspace=self.root/'workspace';r=workspace/'test-run/results';r.mkdir(parents=True)
        for name in ('text.pdf','search.pdf'):(r/name).write_bytes(b'%PDF-1.4\ntest board export')
        manifest={'all_body_pages_processed_by_model':True,'output_label':'test board result',
            'exports':{'text_pdf':'/old-board/text.pdf','searchable_pdf':'/old-board/search.pdf',
                       'pages':2,'text_roundtrip_exact':True,'both_rendered':True}}
        (r/'run_manifest.json').write_text(json.dumps(manifest))
        self.g.results_roots=[workspace]
        self.g.scan_results()
        _,raw=self.get('/api/jobs');jobs=json.loads(raw)['jobs'];self.assertEqual(len(jobs),1)
        self.assertTrue(jobs[0]['imported']);self.assertEqual(jobs[0]['pages'],2)
        self.assertNotIn('source_manifest',jobs[0])
        self.g.scan_results();self.assertEqual(len(self.g.jobs),1)
        # An export being rewritten is not republished until its status is complete.
        (r/'status.json').write_text('{"stage":"export"}')
        manifest['output_label']='not yet ready'
        (r/'run_manifest.json').write_text(json.dumps(manifest))
        self.g.scan_results();self.assertEqual(next(iter(self.g.jobs.values()))['name'],'test board result')
        (r/'status.json').write_text('{"stage":"complete"}')
        self.g.scan_results();self.assertEqual(next(iter(self.g.jobs.values()))['name'],'not yet ready')
        self.assertEqual(self.get(f"/api/jobs/{jobs[0]['id']}/delete", {})[0], 200)
        self.assertTrue((r/'run_manifest.json').exists())
        restarted = module.Gateway(self.g.data, self.recordings, self.runner, sys.executable,
            self.g.camera, self.g.model, self.token, results_roots=[workspace])
        restarted.scan_results()
        self.assertEqual(restarted.jobs, {})

if __name__=='__main__':unittest.main(verbosity=2)
