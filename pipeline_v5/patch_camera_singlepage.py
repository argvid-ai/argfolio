from pathlib import Path
import ast

root=Path(__file__).resolve().parent.parent
src=root/'archives/Phecda_Scan_V2_20261107_V4_Baseline/code/webcam-live/web_cam.py'
s=src.read_text()
def replace(old,new):
    global s
    assert s.count(old)==1,(old[:100],s.count(old))
    s=s.replace(old,new,1)

start=s.index('class HwRecorder:')
pos=s.index('    def is_recording(self):',start)
s=s[:pos]+'''    def crop_for_size(self, w, h):
        if getattr(self, "capture_mode", "single_center") == "full":
            return (0, 0, w, h)
        cw = min(w, max(2, int(h * 32 / 45) // 2 * 2))
        return ((w - cw) // 4 * 2, 0, cw, h // 2 * 2)

    def set_capture_mode(self, mode):
        if mode not in ("single_center", "full"):
            return "请选择居中单页或完整4K画面"
        with self.lock:
            if self.is_recording():
                return "请先停止录制，再切换取景范围"
            self.capture_mode = mode
        return None

'''+s[pos:]
replace('''            path = os.path.join(self.out_dir, name)
            conv_bin = os.path.join(_HERE, "mjpeg_to_nv12")''','''            path = os.path.join(self.out_dir, name)
            crop = self.crop_for_size(w, h)
            cx, cy, cw, ch = crop
            record_size = (cw, ch)
            crop_filter = "crop=%d:%d:%d:%d" % (cw, ch, cx, cy)
            conv_bin = os.path.join(_HERE, "mjpeg_to_nv12")''')
replace('''"-s", "%dx%d" % (w, h), "-r", "%g" % self.fps, "-i", "pipe:0",
                       "-c:v", "h264_v4l2m2m"''','''"-s", "%dx%d" % (w, h), "-r", "%g" % self.fps, "-i", "pipe:0",
                       "-vf", crop_filter,
                       "-c:v", "h264_v4l2m2m"''')
replace('''"-vf", "zscale=filter=point,format=yuv420p",''','''"-vf", crop_filter + ",zscale=filter=point,format=yuv420p",''')
replace('''            self.size = (w, h)
            self.frames.on_jpeg = self._offer''','''            self.size = (w, h)
            self.record_crop = crop
            self.output_size = record_size
            self.frames.on_jpeg = self._offer''')
replace('''args=(self.feed, (w, h), self.stop_evt), daemon=True)''','''args=(self.feed, record_size, self.stop_evt), daemon=True)''')
replace('''return {"file": name, "size": [w, h], "fps": self.fps, "codec": codec}, None''','''return {"file": name, "size": list(record_size), "source_size": [w, h],
                    "crop": list(crop), "fps": self.fps, "codec": codec}, None''')
# Scope metadata to the hardware recorder, leaving the legacy OpenCV recorder intact.
a=s.index('class HwRecorder:');b=s.index('# ---------------------------------------------------------------- 页面',a)
part=s[a:b]
needle='''                    "reason": self.reason,
                }
'''
assert part.count(needle)==1
part=part.replace(needle,'''                    "reason": self.reason,
                    "source_size": list(self.size),
                    "crop": list(self.record_crop),
                    "capture_mode": getattr(self, "capture_mode", "single_center"),
                }
                if self.path:
                    with open(self.path + ".capture.json", "w", encoding="utf-8") as meta:
                        json.dump(self.result, meta, ensure_ascii=False, indent=2)
''')
s=s[:a]+part+s[b:]
replace(''' #livewrap{flex:1}''',''' #livewrap{flex:1;min-height:200px}
 .preview-window{position:relative;overflow:hidden;flex:none;background:#05070a}
 .preview-window img{position:absolute;max-width:none;max-height:none;object-fit:fill}
 .framing{display:flex;align-items:center;gap:12px;padding:10px 14px;flex-wrap:wrap}
 .framing label{font-size:13px;color:var(--dim)}
 .framing select{padding:7px 9px;border:1px solid #2c5138;border-radius:7px;background:#132019;color:#dce8f7}
 .ops{flex-wrap:wrap}''')
replace('''      <div class="view" id="livewrap"><img id="live" src="/stream" alt="实时画面"></div>''','''      <div class="framing"><label for="framing">取景范围</label>
        <select id="framing"><option value="single_center">居中单页</option><option value="full">完整4K画面</option></select>
        <span class="meta" id="sourceinfo">请把单页放入预览范围</span>
      </div>
      <div class="view" id="livewrap"><div class="preview-window" id="previewwindow"><img id="live" src="/stream" alt="实时画面"></div></div>''')
replace('''      <div>分辨率<b id="s_res">—</b></div>''','''      <div>录制分辨率<b id="s_res">—</b></div>''')
replace('''function paintStatus(j){''','''let previewState = null;
function fitPreview(){
  const j=previewState;if(!j)return;
  const wrap=$('livewrap'), win=$('previewwindow'), img=$('live');
  const crop=j.crop||[0,0,j.width,j.height];
  const scale=Math.min(wrap.clientWidth/crop[2],wrap.clientHeight/crop[3]);
  if(!Number.isFinite(scale)||scale<=0)return;
  win.style.width=crop[2]*scale+'px';win.style.height=crop[3]*scale+'px';
  img.style.width=j.width*scale+'px';img.style.height=j.height*scale+'px';
  img.style.left=-crop[0]*scale+'px';img.style.top=-crop[1]*scale+'px';
}
window.addEventListener('resize',fitPreview);
$('framing').onchange=async()=>{
  if(busy||recording)return;busy=true;
  try{
    const j=await api('/capture/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:$('framing').value})});
    if(!j.ok)throw new Error(j.error||'切换失败');
    paintStatus(j);toast(j.capture_mode==='full'?'已切换完整4K画面':'已切换居中单页，请调整书的位置');
  }catch(e){toast(e.message)}finally{busy=false;refresh()}
};
function paintStatus(j){''')
replace('''  $('liveinfo').textContent = (j.width||'?') + '×' + (j.height||'?') + ' · 实时';
  $('s_res').textContent = (j.width||'?') + '×' + (j.height||'?');''','''  const rw=j.record_width||j.width, rh=j.record_height||j.height;
  $('liveinfo').textContent=rw+'×'+rh+' · '+(j.capture_mode==='full'?'完整画面':'居中单页');
  $('s_res').textContent=rw+'×'+rh;
  $('framing').value=j.capture_mode||'full';$('framing').disabled=recording||busy;
  $('sourceinfo').textContent=j.capture_mode==='full'?'录制完整画面':'请把一页放入预览范围，画面外不录入';
  previewState=j;requestAnimationFrame(fitPreview);''')
replace('''    def state(self):
        rec = self.recorder
        started = rec.started_at if rec.is_recording() else None
        return {''','''    def state(self):
        rec = self.recorder
        started = rec.started_at if rec.is_recording() else None
        w, h = self.frames.size()
        crop = rec.crop_for_size(w, h) if hasattr(rec, "crop_for_size") else (0, 0, w, h)
        return {
            "capture_mode": getattr(rec, "capture_mode", "single_center"),
            "crop": list(crop), "record_width": crop[2], "record_height": crop[3],''')
replace('''                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n:
                        self.rfile.read(n)
                except ValueError:
                    pass
                if path == "/record/start":''','''                raw_body = b""
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n:
                        raw_body = self.rfile.read(n)
                except ValueError:
                    pass
                if path == "/capture/mode":
                    try:
                        payload = json.loads(raw_body or b"{}")
                        err = outer.recorder.set_capture_mode(payload.get("mode"))
                    except (ValueError, TypeError, AttributeError):
                        err = "无法设置取景范围"
                    if err:
                        self._json({"ok": False, "error": err}, 409)
                    else:
                        self._json(dict(outer.state(), ok=True))
                    return
                if path == "/record/start":''')
ast.parse(s)
out=root/'pipeline_v5/web_cam_singlepage.py'
out.write_text(s)
print(out)
