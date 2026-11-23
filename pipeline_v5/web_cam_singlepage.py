#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USB 摄像头网页直播 + 网页一键录制（板端运行，浏览器观看/操作）

链路: UVC 摄像头 --(原始 MJPEG)--> 取帧线程 -> 网页直接推送原生 JPEG
                                          -> 录制: MJPEG 转 NV12(跳过 BGR) + 硬件 H.264

4K 摄像头本身是 MJPEG 4:2:2。先用 OpenCV 解成 BGR 再交给 ffmpeg，
软件色彩转换会把录制压到 30 帧以下。NV12 是这块板硬件编码器的快速输入。

用法(板端):
  python3 web_cam.py --http 8090
  浏览器打开程序启动后打印的本机访问地址。

接口:
  GET  /                操作页面
  GET  /stream          MJPEG 实时流
  GET  /status          当前状态(是否录制、时长、分辨率、剩余空间)
  POST /record/start    开始录制
  POST /record/stop     停止录制并封装文件
  GET  /files           录制文件列表
  GET  /media/<name>    播放(?dl=1 则下载), 支持 Range 拖动进度
"""
import argparse
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import queue
import threading
import time
import http.server

import cv2
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
NAME_RE = re.compile(r"^rec_\d{8}_\d{6}(_\d+)?\.mp4$")
GREEN = "#3ddc84"
REC_RED = "#ff4d5e"


# ---------------------------------------------------------------- 取帧
class Frames:
    """保存最新一帧；只留最新帧可避免缓冲区积压导致画面延迟。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.count = 0
        self.w, self.h = 0, 0

    def put(self, frame):
        with self.lock:
            self.frame = frame
            self.count += 1

    def get(self):
        with self.lock:
            return None if self.frame is None else self.frame

    def size(self):
        with self.lock:
            return self.w, self.h


class Capture(threading.Thread):
    def __init__(self, cap, frames, fps):
        super().__init__(daemon=True)
        self.cap, self.frames, self.fps = cap, frames, fps
        self._stop_evt = threading.Event()
        self.times = []

    def run(self):
        while not self._stop_evt.is_set():
            ok, fr = self.cap.read()
            if not ok:
                time.sleep(0.02)
                continue
            self.frames.put(fr)
            now = time.time()
            self.times.append(now)
            if len(self.times) > 60:
                self.times.pop(0)

    def stop(self):
        self._stop_evt.set()

    def measured_fps(self):
        if len(self.times) < 2:
            return 0.0
        span = self.times[-1] - self.times[0]
        return round((len(self.times) - 1) / span, 1) if span > 0 else 0.0


# ---------------------------------------------------------------- MJPEG 编码
class Jpeg:
    """最新一张 JPEG 画面；带序号，推流端只发新帧，不重复刷旧图。"""

    def __init__(self):
        self.cond = threading.Condition()
        self.data = None
        self.seq = 0

    def put(self, data):
        with self.cond:
            self.data = data
            self.seq += 1
            self.cond.notify_all()

    def get(self):
        with self.cond:
            return self.data

    def wait_new(self, last_seq, timeout):
        """等一帧新画面；超时则返回原序号。"""
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            return self.seq, self.data


class Encoder(threading.Thread):
    """按固定帧率把最新帧编码成 JPEG，多个观看者共用同一张图，不重复编码。"""

    def __init__(self, frames, jpeg, width, fps, quality):
        super().__init__(daemon=True)
        self.frames, self.jpeg = frames, jpeg
        self.width, self.fps, self.quality = width, fps, quality
        self._stop_evt = threading.Event()
        self.encoded = 0

    def run(self):
        interval = 1.0 / max(1.0, self.fps)
        next_t = time.time()
        while not self._stop_evt.is_set():
            frame = self.frames.get()
            if frame is None:
                time.sleep(0.01)
                continue
            img = frame
            if self.width and frame.shape[1] > self.width:
                sc = self.width / float(frame.shape[1])
                img = cv2.resize(frame, (self.width, max(1, int(frame.shape[0] * sc))),
                                 interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
            if ok:
                self.jpeg.put(buf.tobytes())
                self.encoded += 1
            next_t += interval
            delay = next_t - time.time()
            if delay < -1.0:
                next_t = time.time()     # 落后太多就重新对齐，避免疯狂追帧
            elif delay > 0:
                self._stop_evt.wait(delay)

    def stop(self):
        self._stop_evt.set()


# ---------------------------------------------------------------- 录制
class Recorder:
    """按固定帧率写入 mp4；优先 H.264(avc1)，浏览器可直接播放。"""

    def __init__(self, frames, out_dir, fps, codec, min_free_gb):
        self.frames, self.out_dir = frames, out_dir
        self.fps, self.codec, self.min_free_gb = fps, codec, min_free_gb
        self.lock = threading.Lock()
        self.thread = None
        self.stop_evt = None
        self.writer = None
        self.path = None
        self.frames_written = 0
        self.started_at = None
        self.result = None
        self.reason = ""

    def is_recording(self):
        t = self.thread
        return t is not None and t.is_alive()

    def free_gb(self):
        try:
            st = os.statvfs(self.out_dir)
            return round(st.f_bavail * st.f_frsize / (1024 ** 3), 1)
        except OSError:
            return 0.0

    def write_fps(self):
        started = self.started_at
        if not started:
            return 0.0
        wall = time.time() - started
        if wall < 0.3:
            return 0.0
        return round(self.frames_written / wall, 1)

    def start(self):
        with self.lock:
            if self.is_recording():
                return None, "已在录制中"
            frame = self.frames.get()
            if frame is None:
                return None, "摄像头暂无画面，请稍后再试"
            h, w = frame.shape[:2]
            base = time.strftime("rec_%Y%m%d_%H%M%S")
            name, i = base + ".mp4", 1
            while os.path.exists(os.path.join(self.out_dir, name)):
                name, i = "%s_%d.mp4" % (base, i), i + 1
            path = os.path.join(self.out_dir, name)
            writer = None
            used = ""
            for cc in (self.codec, "avc1", "mp4v"):
                if not cc:
                    continue
                writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*cc), self.fps, (w, h))
                if writer.isOpened():
                    used = cc
                    break
                writer.release()
                writer = None
            if writer is None:
                return None, "无法创建视频文件(编码器不可用)"
            self.writer, self.path = writer, path
            self.frames_written, self.started_at = 0, time.time()
            self.result, self.reason = None, ""
            self.stop_evt = threading.Event()
            self.thread = threading.Thread(target=self._run,
                                           args=(writer, (w, h), self.stop_evt), daemon=True)
            self.thread.start()
            return {"file": name, "size": [w, h], "fps": self.fps, "codec": used}, None

    def _run(self, writer, size, stop_evt):
        interval = 1.0 / max(1.0, self.fps)
        next_t = time.time()
        next_disk = time.time() + 2.0
        try:
            while not stop_evt.is_set():
                frame = self.frames.get()
                if frame is not None:
                    if (frame.shape[1], frame.shape[0]) != size:
                        frame = cv2.resize(frame, size)
                    writer.write(frame)
                    self.frames_written += 1
                next_t += interval
                delay = next_t - time.time()
                if delay < -0.5:
                    next_t = time.time()
                elif delay > 0:
                    time.sleep(delay)
                if time.time() >= next_disk:
                    next_disk = time.time() + 2.0
                    if self.free_gb() < self.min_free_gb:
                        self.reason = "磁盘剩余空间不足，已自动停止录制"
                        break
        finally:
            secs = max(0.0, time.time() - self.started_at)
            writer.release()
            with self.lock:
                self.writer = None
                size_mb = round(os.path.getsize(self.path) / 1048576.0, 2) if self.path and os.path.exists(self.path) else 0.0
                self.result = {
                    "file": os.path.basename(self.path) if self.path else "",
                    "seconds": round(secs, 1),
                    "frames": self.frames_written,
                    "size_mb": size_mb,
                    "width": size[0], "height": size[1], "fps": self.fps,
                    "reason": self.reason,
                }

    def stop(self):
        """停止录制并等待文件封装完成(释放 VideoWriter 后 mp4 的索引才会写完)。"""
        t = self.thread
        if t is None:
            return None
        if self.stop_evt is not None:
            self.stop_evt.set()
        t.join(timeout=15.0)
        return self.result


# ---------------------------------------------------------------- 原始 MJPEG 直通
class RawFrames:
    """保存摄像头原始 JPEG 帧；推流直接用它，不需要解码。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.jpeg = None
        self.seq = 0
        self.w, self.h = 0, 0
        self.times = []
        self.on_jpeg = None

    def put(self, data, w, h):
        now = time.time()
        with self.lock:
            self.jpeg, self.w, self.h = data, w, h
            self.seq += 1
            self.times.append(now)
            if len(self.times) > 60:
                self.times.pop(0)
            callback = self.on_jpeg
        if callback is not None:
            callback(data, w, h)

    def latest(self):
        with self.lock:
            return self.jpeg, self.w, self.h, self.seq

    def get(self):
        with self.lock:
            return self.jpeg

    def size(self):
        with self.lock:
            return self.w, self.h

    def measured_fps(self):
        with self.lock:
            if len(self.times) < 2:
                return 0.0
            span = self.times[-1] - self.times[0]
            return round((len(self.times) - 1) / span, 1) if span > 0 else 0.0


class RawCapture(threading.Thread):
    """用 v4l2-ctl 把摄像头原生 MJPEG 裸流读到内存并按 JPEG 边界切帧。

    不经过 OpenCV 解码，2K 实测可稳定拿到 30 fps 以上（软件解码只有 12.5 fps）。
    摄像头拔插后会自动重新识别节点并重连。
    """

    def __init__(self, src, width, height, fps, fourcc, frames, jpeg):
        super().__init__(daemon=True)
        self.src, self.width, self.height = src, width, height
        self.fps, self.fourcc = fps, fourcc
        self.frames, self.jpeg = frames, jpeg
        self._stop_evt = threading.Event()
        self.proc = None
        self.node = None
        self._printed = None

    def run(self):
        buf = bytearray()
        warned = False
        while not self._stop_evt.is_set():
            node = self._pick_node()
            if node is None:
                self._stop_evt.wait(1.5)
                continue
            cmd = ["v4l2-ctl", "-d", node,
                   "--set-fmt-video=width=%d,height=%d,pixelformat=%s"
                   % (self.width, self.height, self.fourcc),
                   "--set-parm=%d" % self.fps,
                   "--stream-mmap=4", "--stream-to=-"]
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0)
            except OSError as e:
                if not warned:
                    print("[cam] 启动 v4l2-ctl 失败: %s" % e, flush=True)
                    warned = True
                self._stop_evt.wait(1.5)
                continue
            self.proc, self.node = p, node
            if node != self._printed:
                print("[cam] %s 采集 %dx%d@%d 原始MJPEG"
                      % (node, self.width, self.height, self.fps), flush=True)
                self._printed = node
            buf.clear()
            try:
                while not self._stop_evt.is_set():
                    chunk = p.stdout.read(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    self._split(buf)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    p.terminate()
                    p.wait(timeout=2)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
                self.proc = None
            self._stop_evt.wait(1.0)

    def _split(self, buf):
        """从字节流里切出完整 JPEG 帧（FFD8FF 开头, FFD9 结尾）。"""
        while True:
            s = buf.find(b"\xff\xd8\xff")
            if s < 0:
                if len(buf) > 2:
                    del buf[:-2]
                return
            if s:
                del buf[:s]
            e = buf.find(b"\xff\xd9", 3)
            if e < 0:
                if len(buf) > 8 * 1024 * 1024:
                    buf.clear()
                return
            data = bytes(buf[:e + 2])
            del buf[:e + 2]
            w, h = self._dim(data) or (self.width, self.height)
            self.frames.put(data, w, h)
            self.jpeg.put(data)

    @staticmethod
    def _dim(data):
        for mk in (b"\xff\xc0", b"\xff\xc2"):
            i = data.find(mk, 2)
            if 0 <= i and i + 9 <= len(data):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                if w and h:
                    return w, h
        return None

    def _pick_node(self):
        for cand in _camera_candidates(self.src):
            if os.path.exists(cand):
                return cand
        return None

    def measured_fps(self):
        return self.frames.measured_fps()

    def stop(self):
        self._stop_evt.set()
        p = self.proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass


def _widen_pipe(pipe):
    try:
        import fcntl
        flag = getattr(fcntl, "F_SETPIPE_SZ", 1031)
        fcntl.fcntl(pipe.fileno(), flag, 1 << 20)
    except (OSError, AttributeError):
        pass


def _drain_pipe(pipe):
    try:
        while pipe.read(4096):
            pass
    except Exception:
        pass


class HwRecorder:
    """录制：原始 JPEG 转成 NV12 后交给硬件 H.264。

    4K 实测这条链路约 47 fps，直接 cv2.imdecode 成 BGR 再编码到不了 30 fps。
    转换程序不在时，退回 ffmpeg 的 zscale。
    """

    def __init__(self, frames, out_dir, fps, min_free_gb, bitrate):
        self.frames, self.out_dir = frames, out_dir
        self.fps, self.min_free_gb, self.bitrate = fps, min_free_gb, bitrate
        self.lock = threading.Lock()
        self.thread = None
        self.stop_evt = None
        self.proc = None
        self.conv = None
        self.feed = None
        self.feed_lock = threading.Lock()
        self.pending = queue.Queue(maxsize=4)
        self.size = (0, 0)
        self.path = None
        self.frames_written = 0
        self.started_at = None
        self.result = None
        self.reason = ""

    def crop_for_size(self, w, h):
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

    def is_recording(self):
        t = self.thread
        return t is not None and t.is_alive()

    def free_gb(self):
        try:
            st = os.statvfs(self.out_dir)
            return round(st.f_bavail * st.f_frsize / (1024 ** 3), 1)
        except OSError:
            return 0.0

    def write_fps(self):
        started = self.started_at
        if not started:
            return 0.0
        wall = time.time() - started
        if wall < 0.3:
            return 0.0
        return round(self.frames_written / wall, 1)

    def start(self):
        with self.lock:
            if self.is_recording():
                return None, "已在录制中"
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                return None, "未找到 ffmpeg，无法硬件编码"
            data, w, h, _ = self.frames.latest()
            if data is None or w < 2 or h < 2:
                return None, "摄像头暂无画面，请稍后再试"
            base = time.strftime("rec_%Y%m%d_%H%M%S")
            name, i = base + ".mp4", 1
            while os.path.exists(os.path.join(self.out_dir, name)):
                name, i = "%s_%d.mp4" % (base, i), i + 1
            path = os.path.join(self.out_dir, name)
            crop = self.crop_for_size(w, h)
            cx, cy, cw, ch = crop
            record_size = (cw, ch)
            crop_filter = "crop=%d:%d:%d:%d" % (cw, ch, cx, cy)
            conv_bin = os.path.join(_HERE, "mjpeg_to_nv12")
            use_nv12 = os.access(conv_bin, os.X_OK)
            conv = None
            if use_nv12:
                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-f", "rawvideo", "-pix_fmt", "nv12",
                       "-s", "%dx%d" % (w, h), "-r", "%g" % self.fps, "-i", "pipe:0",
                       "-vf", crop_filter,
                       "-c:v", "h264_v4l2m2m", "-b:v", self.bitrate,
                       "-bf", "0", "-g", "30",
                       "-movflags", "+faststart", path]
            else:
                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-f", "mjpeg", "-framerate", "%g" % self.fps, "-i", "pipe:0",
                       "-vf", crop_filter + ",zscale=filter=point,format=yuv420p",
                       "-c:v", "h264_v4l2m2m", "-b:v", self.bitrate,
                       "-bf", "0", "-g", "30",
                       "-movflags", "+faststart", path]
            try:
                prefix = ["taskset", "-c", "0,1"] if shutil.which("taskset") else []
                if use_nv12:
                    conv = subprocess.Popen(prefix + [conv_bin], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            bufsize=0)
                    p = subprocess.Popen(prefix + cmd, stdin=conv.stdout, stderr=subprocess.PIPE, bufsize=0)
                    conv.stdout.close()
                    _widen_pipe(conv.stdin)
                else:
                    p = subprocess.Popen(prefix + cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                    _widen_pipe(p.stdin)
            except OSError as e:
                if conv is not None:
                    conv.kill()
                return None, "启动编码器失败: %s" % e
            time.sleep(0.4)
            if p.poll() is not None or (conv is not None and conv.poll() is not None):
                msg = ""
                if p.stderr:
                    try:
                        msg = p.stderr.read().decode("utf-8", "ignore").strip()
                    except Exception:
                        msg = ""
                if conv is not None and conv.poll() is None:
                    conv.kill()
                if p.poll() is None:
                    p.kill()
                return None, "硬件编码器不可用: %s" % (msg.splitlines()[-1] if msg else "启动失败")
            self.proc, self.conv, self.path = p, conv, path
            self.feed = conv.stdin if conv is not None else p.stdin
            self.size = (w, h)
            self.record_crop = crop
            self.output_size = record_size
            self.frames.on_jpeg = self._offer
            if p.stderr is not None:
                threading.Thread(target=_drain_pipe, args=(p.stderr,), daemon=True).start()
            self.frames_written, self.started_at = 0, time.time()
            self.result, self.reason = None, ""
            self.stop_evt = threading.Event()
            self.thread = threading.Thread(target=self._run,
                                           args=(self.feed, record_size, self.stop_evt), daemon=True)
            self.thread.start()
            codec = "h264_v4l2m2m+nv12" if use_nv12 else "h264_v4l2m2m+zscale"
            return {"file": name, "size": list(record_size), "source_size": [w, h],
                    "crop": list(crop), "fps": self.fps, "codec": codec}, None

    def _offer(self, data, w, h):
        if (w, h) != self.size:
            return
        try:
            self.pending.put_nowait(data)
        except queue.Full:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                pass
            try:
                self.pending.put_nowait(data)
            except queue.Full:
                pass

    def _run(self, feed, size, stop_evt):
        next_disk = time.time() + 2.0
        try:
            while not stop_evt.is_set():
                try:
                    data = self.pending.get(timeout=0.2)
                except queue.Empty:
                    data = None
                if data is not None:
                    with self.feed_lock:
                        if self.feed is None:
                            break
                        try:
                            view = memoryview(data)
                            while view:
                                n = feed.write(view)
                                if not n:
                                    raise BrokenPipeError
                                view = view[n:]
                            self.frames_written += 1
                        except (BrokenPipeError, OSError):
                            self.reason = self.reason or "编码器已退出"
                            self.feed = None
                            break
                proc = self.proc
                if proc is not None and proc.poll() is not None:
                    self.reason = self.reason or "编码器意外退出"
                    break
                if time.time() >= next_disk:
                    next_disk = time.time() + 2.0
                    if self.free_gb() < self.min_free_gb:
                        self.reason = "磁盘剩余空间不足，已自动停止录制"
                        break
        finally:
            wall = max(0.001, time.time() - self.started_at)
            self.frames.on_jpeg = None
            with self.feed_lock:
                self.feed = None
            try:
                feed.close()
            except Exception:
                pass
            conv, proc = self.conv, self.proc
            if conv is not None:
                try:
                    conv.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    conv.kill()
            if proc is not None:
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
            with self.lock:
                self.proc = None
                self.conv = None
                self.feed = None
                size_mb = 0.0
                if self.path and os.path.exists(self.path):
                    size_mb = round(os.path.getsize(self.path) / 1048576.0, 2)
                self.result = {
                    "file": os.path.basename(self.path) if self.path else "",
                    "seconds": round(wall, 1),
                    "frames": self.frames_written,
                    "write_fps": round(self.frames_written / wall, 1),
                    "size_mb": size_mb,
                    "width": size[0], "height": size[1], "fps": self.fps,
                    "reason": self.reason,
                    "source_size": list(self.size),
                    "crop": list(self.record_crop),
                    "capture_mode": getattr(self, "capture_mode", "single_center"),
                }
                if self.path:
                    with open(self.path + ".capture.json", "w", encoding="utf-8") as meta:
                        json.dump(self.result, meta, ensure_ascii=False, indent=2)

    def stop(self):
        t = self.thread
        if t is None:
            return None
        if self.stop_evt is not None:
            self.stop_evt.set()
        t.join(timeout=30.0)
        return self.result


# ---------------------------------------------------------------- 页面
PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radxa O6 · 摄像头直播录制</title>
<style>
 :root{--bg:#07090c;--panel:#11151b;--line:#202833;--fg:#eef4ff;--dim:#8d9bab;--acc:#3ddc84;--red:#ff4d5e}
 *{box-sizing:border-box}
 html,body{margin:0;height:100%}
 body{background:radial-gradient(1200px 600px at 50% -10%,#12241b,#06080a 60%);
   color:var(--fg);font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
   display:flex;flex-direction:column;overflow:hidden}
 header{display:flex;align-items:center;gap:14px;padding:13px 22px;border-bottom:1px solid var(--line)}
 .dot{width:9px;height:9px;border-radius:50%;background:var(--acc);box-shadow:0 0 12px var(--acc)}
 header h1{font-size:17px;margin:0;font-weight:600;letter-spacing:.5px}
 .tag{font-size:12px;color:var(--dim);border:1px solid var(--line);padding:3px 9px;border-radius:99px}
 .grow{flex:1}
 main{flex:1;display:grid;grid-template-columns:1.55fr 1fr;gap:14px;padding:14px 22px;min-height:0}
 .col{display:flex;flex-direction:column;gap:14px;min-height:0}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
   display:flex;flex-direction:column;overflow:hidden;min-height:0}
 .card h2{margin:0;padding:10px 14px;font-size:13px;font-weight:500;color:var(--dim);
   border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px}
 .card h2 em{font-style:normal;font-size:11px;color:#73c795;margin-left:8px;
   border:1px solid #274734;padding:1px 7px;border-radius:99px}
 .view{flex:1;display:flex;align-items:center;justify-content:center;background:#05070a;min-height:0}
 .view img,.view video{max-width:100%;max-height:100%;object-fit:contain;display:block}
 #livewrap{flex:1;min-height:clamp(200px,36vh,420px)}
 .preview-window{position:relative;overflow:hidden;flex:none;background:#05070a}
 .preview-window img{position:absolute;max-width:none;max-height:none;object-fit:fill}
 .framing{display:flex;align-items:center;gap:12px;padding:10px 14px;flex-wrap:wrap}
 .framing label{font-size:13px;color:var(--dim)}
 .framing select{padding:7px 9px;border:1px solid #2c5138;border-radius:7px;background:#132019;color:#dce8f7}
 .ops{flex-wrap:wrap}
 .ops{display:flex;gap:16px;padding:14px;border-top:1px solid var(--line);align-items:center}
 button{flex:0 0 auto;padding:13px 26px;font-size:15px;font-weight:700;border-radius:10px;
   border:none;cursor:pointer;color:#04140a;letter-spacing:1px;
   background:linear-gradient(180deg,#4ee08a,#22a45a);
   box-shadow:0 6px 18px rgba(61,220,132,.22);transition:transform .08s,filter .15s}
 button:hover{filter:brightness(1.06)} button:active{transform:translateY(1px)}
 button:disabled{opacity:.45;cursor:progress;box-shadow:none}
 button.rec{background:linear-gradient(180deg,#ff7a86,#d92f43);color:#fff;
   box-shadow:0 6px 18px rgba(255,77,94,.28)}
 .meta{font-size:12.5px;color:var(--dim);line-height:1.6}
 .recbar{display:none;align-items:center;gap:9px;color:#ffd7db;font-size:14px}
 .recbar.on{display:flex}
 .recbar .r{width:10px;height:10px;border-radius:50%;background:var(--red);
   box-shadow:0 0 10px var(--red);animation:blink 1s infinite}
 @keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
 .stats{display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;padding:14px 16px;
   font-size:12.5px;color:var(--dim)}
 .stats b{display:block;color:#d7e6ff;font-size:13px;font-weight:500;margin-top:2px}
 .files{flex:1;overflow:auto;padding:6px 10px 12px;min-height:0}
 .empty{color:#5f7285;font-size:13px;text-align:center;line-height:1.8;padding:28px 20px}
 .frow{display:flex;align-items:center;gap:10px;padding:9px 8px;border-radius:9px;
   border-bottom:1px solid rgba(32,40,51,.7);font-size:13px}
 .frow:last-child{border-bottom:none}
 .frow:hover{background:rgba(61,220,132,.06)}
 .frow .nm{flex:1;color:#dce8f7;word-break:break-all;font-variant-numeric:tabular-nums}
 .frow .sz{color:var(--dim);font-size:12px;white-space:nowrap}
 .frow a{padding:5px 10px;border-radius:7px;font-size:12px;text-decoration:none;
   color:#9fe8bd;border:1px solid #2c5138;background:#132019}
 .frow a:hover{background:#1b2f23}
 .frow a.dl{color:#a9c6ff;border-color:#2b3f63;background:#121a28}
 #toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);
   background:#132019;border:1px solid #2c5138;color:#c9f5d9;padding:11px 20px;border-radius:10px;
   font-size:13px;opacity:0;pointer-events:none;transition:.25s}
 #toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
 #player{display:none;padding:10px}
 #player.on{display:flex}
 @media(max-width:900px){main{grid-template-columns:1fr;grid-template-rows:auto auto 1fr}}
</style></head>
<body>
<header>
  <span class="dot"></span>
  <h1>Radxa O6 · 摄像头直播录制</h1>
  <span class="tag">EMEET C60E</span>
  <span class="tag" id="camtag">连接中…</span>
  <span class="grow"></span>
  <span class="tag" id="stat">—</span>
</header>
<main>
  <div class="col">
    <section class="card" id="livecard">
      <h2><span>实时画面</span><span id="liveinfo">—</span></h2>
      <div class="framing"><label for="framing">取景范围</label>
        <select id="framing"><option value="single_center">居中单页</option><option value="full">完整4K画面</option></select>
        <span class="meta" id="sourceinfo">请把单页放入预览范围</span>
      </div>
      <div class="view" id="livewrap"><div class="preview-window" id="previewwindow"><img id="live" src="/stream" alt="实时画面"></div></div>
      <div class="ops">
        <button id="btn">开始录制</button>
        <div class="recbar" id="recbar"><span class="r"></span><span>正在录制</span><b id="elapsed">00:00</b></div>
        <div class="meta" id="meta">画面实时预览，点击按钮开始录制；再次点击停止并保存视频。</div>
      </div>
    </section>
    <section class="card" id="playcard" style="display:none">
      <h2><span>最近一次录制</span><span id="playinfo">—</span></h2>
      <div class="view" id="player"><video id="video" controls playsinline></video></div>
    </section>
  </div>
  <section class="card">
    <h2><span>录制文件<em>保存于板端</em></span><span id="freeinfo">—</span></h2>
    <div class="stats">
      <div>录制分辨率<b id="s_res">—</b></div>
      <div>摄像头帧率<b id="s_camfps">—</b></div>
      <div>推流帧率<b id="s_streamfps">—</b></div>
      <div>录制帧率<b id="s_recfps">—</b></div>
    </div>
    <div class="files" id="files"><div class="empty">加载中…</div></div>
  </section>
</main>
<div id="toast"></div>
<script>
const $ = id => document.getElementById(id);
let recording = false, busy = false, lastResult = null;

function toast(msg){
  const t = $('toast'); t.textContent = msg; t.classList.add('on');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), 2600);
}
const fmt = s => String(Math.floor(s/60)).padStart(2,'0') + ':' + String(Math.floor(s%60)).padStart(2,'0');
function esc(s){ return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function api(path, opt){
  const r = await fetch(path, Object.assign({cache:'no-store'}, opt));
  return await r.json();
}

async function toggle(){
  if (busy) return;
  busy = true;
  const b = $('btn');
  b.disabled = true;
  try{
    if (!recording){
      b.textContent = '正在启动…';
      const j = await api('/record/start', {method:'POST'});
      if (!j.ok) throw new Error(j.error || '开始失败');
      recording = true; refresh();
      toast('已开始录制 ' + j.file);
    }else{
      b.textContent = '正在保存…';
      const j = await api('/record/stop', {method:'POST'});
      if (!j.ok) throw new Error(j.error || '停止失败');
      recording = false;
      lastResult = j;
      $('playcard').style.display = 'flex';
      $('player').classList.add('on');
      const v = $('video');
      v.src = '/media/' + encodeURIComponent(j.file) + '?t=' + Date.now();
      $('playinfo').textContent = j.seconds + ' s · ' + j.size_mb + ' MB';
      refresh(); loadFiles();
      toast('录制完成：' + j.file + '（' + j.size_mb + ' MB）');
    }
  }catch(e){
    toast('操作失败：' + e.message);
  }finally{
    busy = false; b.disabled = false;
  }
}

let previewState = null;
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
function paintStatus(j){
  recording = !!j.recording;
  const b = $('btn');
  b.textContent = recording ? '停止录制' : '开始录制';
  b.classList.toggle('rec', recording);
  $('recbar').classList.toggle('on', recording);
  $('elapsed').textContent = fmt(j.elapsed || 0);
  $('stat').textContent = recording ? '录制中 · ' + fmt(j.elapsed || 0) : '待机';
  $('camtag').textContent = (j.width||'?') + '×' + (j.height||'?') ;
  const rw=j.record_width||j.width, rh=j.record_height||j.height;
  $('liveinfo').textContent=rw+'×'+rh+' · '+(j.capture_mode==='full'?'完整画面':'居中单页');
  $('s_res').textContent=rw+'×'+rh;
  $('framing').value=j.capture_mode||'full';$('framing').disabled=recording||busy;
  $('sourceinfo').textContent=j.capture_mode==='full'?'录制完整画面':'请把一页放入预览范围，画面外不录入';
  previewState=j;requestAnimationFrame(fitPreview);
  $('s_camfps').textContent = (j.cam_fps||0) + ' fps';
  $('s_streamfps').textContent = (j.stream_fps||0) + ' fps';
  const shown = (j.recording && j.write_fps) ? j.write_fps : (j.write_fps || j.record_fps || 0);
  $('s_recfps').textContent = shown + ' fps';
  $('freeinfo').textContent = '剩余 ' + (j.free_gb||0) + ' GB';
  const m = $('meta');
  if (recording){
    m.textContent = '正在录制到 ' + (j.file||'') + ' ，录制中可继续观看实时画面。';
  }else if (j.last){
    m.textContent = j.last.file + ' · ' + j.last.seconds + ' s · ' + j.last.size_mb + ' MB'
      + (j.last.reason ? '（' + j.last.reason + '）' : '');
  }else{
    m.textContent = '画面实时预览，点击按钮开始录制；再次点击停止并保存视频。';
  }
}

async function refresh(){
  try{ paintStatus(await api('/status')); }catch(e){ $('stat').textContent = '未连接'; }
}

async function loadFiles(){
  try{
    const j = await api('/files');
    const list = $('files');
    if (!j.files.length){ list.innerHTML = '<div class="empty">还没有录制文件<br>点击「开始录制」生成第一段视频</div>'; return; }
    list.innerHTML = j.files.map(f =>
      '<div class="frow"><span class="nm">' + esc(f.name) + '</span>' +
      '<span class="sz">' + f.size_mb + ' MB</span>' +
      '<a href="/media/' + encodeURIComponent(f.name) + '?t=' + Date.now() + '" target="_blank">播放</a>' +
      '<a class="dl" href="/media/' + encodeURIComponent(f.name) + '?dl=1">下载</a></div>').join('');
  }catch(e){ $('files').innerHTML = '<div class="empty">读取失败</div>'; }
}

$('btn').onclick = toggle;
$('live').onerror = () => setTimeout(() => { $('live').src = '/stream?t=' + Date.now(); }, 1200);
setInterval(() => { if (!busy) refresh(); }, 700);
setInterval(loadFiles, 5000);
refresh(); loadFiles();
</script>
</body></html>
"""


# ---------------------------------------------------------------- HTTP 服务
class Server(threading.Thread):
    def __init__(self, port, frames, jpeg, recorder, out_dir, camfps, streamfps, recfps):
        super().__init__(daemon=True)
        self.port, self.frames, self.jpeg = port, frames, jpeg
        self.recorder, self.out_dir = recorder, out_dir
        self.camfps, self.streamfps, self.recfps = camfps, streamfps, recfps
        self.srv = None
        self._ready = threading.Event()
        self.stop_event = threading.Event()

    def state(self):
        rec = self.recorder
        started = rec.started_at if rec.is_recording() else None
        w, h = self.frames.size()
        crop = rec.crop_for_size(w, h) if hasattr(rec, "crop_for_size") else (0, 0, w, h)
        return {
            "capture_mode": getattr(rec, "capture_mode", "single_center"),
            "crop": list(crop), "record_width": crop[2], "record_height": crop[3],
            "recording": rec.is_recording(),
            "elapsed": round(time.time() - started, 1) if started else 0,
            "file": os.path.basename(rec.path) if rec.is_recording() and rec.path else "",
            "width": self.frames.size()[0],
            "height": self.frames.size()[1],
            "cam_fps": self.camfps(),
            "stream_fps": self.streamfps,
            "record_fps": self.recfps,
            "write_fps": rec.write_fps() if rec.is_recording() else (rec.result or {}).get("write_fps", 0),
            "free_gb": rec.free_gb(),
            "last": rec.result,
        }

    def list_files(self):
        out = []
        try:
            for name in sorted(os.listdir(self.out_dir), reverse=True):
                if not NAME_RE.match(name):
                    continue
                p = os.path.join(self.out_dir, name)
                st = os.stat(p)
                out.append({"name": name,
                            "size_mb": round(st.st_size / 1048576.0, 2),
                            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
        except OSError:
            pass
        return out

    def run(self):
        import http.server
        import socketserver
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                try:
                    self.connection.settimeout(6.0)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

            def _send(self, code, ctype, body, extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, "application/json; charset=utf-8",
                           json.dumps(obj, ensure_ascii=False).encode("utf-8"))

            def _file(self, path, offset, length):
                with open(path, "rb") as f:
                    f.seek(offset)
                    while length > 0:
                        data = f.read(min(256 * 1024, length))
                        if not data:
                            break
                        self.wfile.write(data)
                        length -= len(data)

            def _media(self, name, download):
                path = os.path.join(outer.out_dir, name)
                if not NAME_RE.match(name) or not os.path.isfile(path):
                    self.send_error(404)
                    return
                size = os.path.getsize(path)
                ctype = "video/mp4"
                disp = {"Content-Disposition": 'attachment; filename="%s"' % name} if download else {}
                rng = self.headers.get("Range", "")
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m and not download:
                    a = int(m.group(1)) if m.group(1) else 0
                    b = int(m.group(2)) if m.group(2) else size - 1
                    a, b = max(0, a), min(size - 1, b)
                    if a > b:
                        self.send_error(416)
                        return
                    length = b - a + 1
                    self.send_response(206)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, size))
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    for k, v in disp.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self._file(path, a, length)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                for k, v in disp.items():
                    self.send_header(k, v)
                self.end_headers()
                self._file(path, 0, size)

            def do_GET(self):
                path = self.path.split("?")[0]
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                if path in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
                elif path == "/status":
                    self._json(outer.state())
                elif path == "/health":
                    self._json({"elapsed": outer.state()["elapsed"]})
                elif path == "/files":
                    self._json({"files": outer.list_files()})
                elif path.startswith("/media/"):
                    import urllib.parse
                    name = urllib.parse.unquote(path[len("/media/"):])
                    self._media(name, download="dl=1" in qs)
                elif path == "/snapshot.jpg":
                    data = outer.jpeg.get()
                    if data is None:
                        self.send_error(503, "no frame yet")
                    else:
                        self._send(200, "image/jpeg", data)
                elif path == "/stream":
                    self.send_response(200)
                    self.send_header("Age", "0")
                    self.send_header("Cache-Control", "no-cache, private")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
                    self.end_headers()
                    interval = 1.0 / max(1.0, outer.streamfps)
                    last_seq, last_t = -1, 0.0
                    try:
                        while not cells_stop(outer):
                            seq, data = outer.jpeg.wait_new(last_seq, 0.5)
                            if data is None or seq == last_seq:
                                continue
                            last_seq = seq
                            now = time.time()
                            if now - last_t < interval * 0.98:
                                continue          # 按推流帧率丢弃多余帧
                            last_t = now
                            self.wfile.write(b"--FRAME\r\n")
                            self.wfile.write(b"Content-Type: image/jpeg\r\n")
                            self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(data))
                            self.wfile.write(data)
                            self.wfile.write(b"\r\n")
                    except OSError:
                        pass
                else:
                    self.send_error(404)

            def do_POST(self):
                path = self.path.split("?")[0]
                raw_body = b""
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
                if path == "/record/start":
                    info, err = outer.recorder.start()
                    if err:
                        self._json({"ok": False, "error": err}, 409)
                    else:
                        self._json(dict(info, ok=True))
                    return
                if path == "/record/stop":
                    res = outer.recorder.stop()
                    if not res:
                        self._json({"ok": False, "error": "当前没有在录制"}, 409)
                    else:
                        self._json(dict(res, ok=True))
                    return
                self.send_error(404)

        class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request, client_address):
                # 浏览器关页/切换网络时的 connection reset 属正常现象，不打印堆栈
                pass

            def server_bind(self):
                # 绕过 HTTPServer 里的 getfqdn() 反向解析：板端 DNS 不通时会卡住几十秒
                socketserver.TCPServer.server_bind(self)
                host, port = self.server_address[:2]
                self.server_name = host
                self.server_port = port

        self.srv = ThreadedServer(("0.0.0.0", self.port), Handler)
        self._ready.set()
        try:
            self.srv.serve_forever(poll_interval=0.2)
        finally:
            self.srv.server_close()

    def stop(self):
        self.stop_event.set()
        if self._ready.wait(timeout=3.0) and self.srv is not None:
            try:
                self.srv.shutdown()
            except Exception:
                pass


def cells_stop(srv):
    return srv.stop_event.is_set()


def _local_ips():
    """取本机非回环 IPv4，不访问外部探测服务器。"""
    import subprocess
    ips = []
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True, timeout=3).split()
        ips = [ip for ip in out if not ip.startswith("127.") and ":" not in ip]
    except Exception:
        pass
    if not ips:
        try:
            ips = [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2]
                   if not ip.startswith("127.")]
        except OSError:
            pass
    return ips or ["127.0.0.1"]


def build_camera(args):
    """打开摄像头。--cam auto 时按 sysfs 自动找采集节点(插拔后编号会变)。"""
    cap = None
    src = ""
    for cand in _camera_candidates(args.cam):
        c = cv2.VideoCapture(cand, cv2.CAP_V4L2)
        if not c.isOpened():
            c.release()
            continue
        c.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*args.fourcc))
        c.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        c.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        c.set(cv2.CAP_PROP_FPS, args.fps)
        c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        for _ in range(max(0, args.warmup)):
            c.read()
        ok, fr = c.read()
        if ok:
            cap, src = c, cand
            break
        c.release()
    if cap is None:
        raise SystemExit("[err] 没找到可用的摄像头，检查是否插好或指定 --cam /dev/videoX")
    print("[cam] %s 请求 %dx%d@%d, 实测 %dx%d"
          % (src, args.width, args.height, args.fps, fr.shape[1], fr.shape[0]), flush=True)
    return cap, fr


def _camera_candidates(src):
    """返回待尝试的节点列表: 显式路径优先; auto 时枚举 index=0 的采集节点。"""
    if str(src).isdigit():
        return ["/dev/video" + str(src)]
    if src and src != "auto":
        return [src]
    import glob
    nodes, preferred = [], []
    for path in sorted(glob.glob("/dev/video*")):
        b = os.path.basename(path)
        try:
            name = open("/sys/class/video4linux/%s/name" % b).read().strip()
            idx = open("/sys/class/video4linux/%s/index" % b).read().strip()
        except OSError:
            continue
        if idx != "0":
            continue          # index=1 一般是 metadata 节点, 不能取画面
        nodes.append(path)
        if any(k in name for k in ("SmartCam", "EMEET", "Camera", "USB")) \
                and "Metadata" not in name:
            preferred.append(path)
    return preferred + [p for p in nodes if p not in preferred]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", default="auto", help="auto(自动识别) 或 /dev/videoX")
    ap.add_argument("--capture", default="raw", choices=("raw", "opencv"),
                    help="raw=原始MJPEG直通(推荐, 2K可到30fps) / opencv=软件解码")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30, help="摄像头采集帧率")
    ap.add_argument("--fourcc", default="MJPG", help="摄像头像素格式")
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--http", type=int, default=8090, help="网页端口")
    ap.add_argument("--stream-width", type=int, default=0,
                    help="opencv模式下推流缩放宽度; raw模式忽略(用原生分辨率)")
    ap.add_argument("--stream-fps", type=float, default=30.0, help="网页推流帧率上限")
    ap.add_argument("--jpeg-quality", type=int, default=80, help="opencv模式重编码质量")
    ap.add_argument("--record-fps", type=float, default=30.0,
                    help="录制视频帧率(与摄像头实际帧率接近可减少重复帧)")
    ap.add_argument("--codec", default="avc1", help="opencv模式编码器: avc1 / mp4v")
    ap.add_argument("--bitrate", default="10M", help="硬件H.264录制码率")
    ap.add_argument("--out-dir", default=os.path.join(_HERE, "recordings"))
    ap.add_argument("--min-free-gb", type=float, default=1.0, help="低于该剩余空间自动停录")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    jpeg = Jpeg()
    encoder = None
    cap = None

    if args.capture == "raw" and not shutil.which("v4l2-ctl"):
        print("[warn] 未找到 v4l2-ctl，回退 OpenCV 采集模式", flush=True)
        args.capture = "opencv"

    if args.capture == "raw":
        if args.stream_width:
            print("[warn] raw 模式忽略 --stream-width，推流使用摄像头原生分辨率", flush=True)
        frames = RawFrames()
        capture = RawCapture(args.cam, args.width, args.height, args.fps, args.fourcc, frames, jpeg)
        capture.start()
        recorder = HwRecorder(frames, args.out_dir, args.record_fps, args.min_free_gb, args.bitrate)
        print("[mode] 原始MJPEG直通: 推流零转码, 录制 NV12 + 硬件H.264", flush=True)
    else:
        cap, first = build_camera(args)
        frames = Frames()
        frames.put(first)
        frames.w, frames.h = first.shape[1], first.shape[0]
        capture = Capture(cap, frames, args.fps)
        capture.start()
        encoder = Encoder(frames, jpeg, args.stream_width, args.stream_fps, args.jpeg_quality)
        encoder.start()
        recorder = Recorder(frames, args.out_dir, args.record_fps, args.codec, args.min_free_gb)
        print("[mode] OpenCV 软件解码采集", flush=True)

    server = Server(args.http, frames, jpeg, recorder, args.out_dir,
                    capture.measured_fps, args.stream_fps, args.record_fps)
    server.start()

    print("[http] 打开 http://%s:%d/ 观看直播并录制" % (_local_ips()[0], args.http), flush=True)
    for ip in _local_ips():
        print("[http] 或 http://%s:%d/" % (ip, args.http), flush=True)
    print("[rec] 录制文件目录: %s" % args.out_dir, flush=True)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except Exception:
            pass

    try:
        while not stop.wait(0.5):
            pass
    finally:
        print("[stop] 正在收尾…", flush=True)
        if recorder.is_recording():
            recorder.stop()
            print("[stop] 已结束并保存当前录制", flush=True)
        server.stop()
        if encoder is not None:
            encoder.stop()
        capture.stop()
        capture.join(timeout=3.0)
        if cap is not None and not capture.is_alive():
            cap.release()
        if encoder is not None:
            encoder.join(timeout=2.0)
        server.join(timeout=2.0)
        print("[stop] 退出", flush=True)
        sys.stdout.flush()
        if capture.is_alive() or server.is_alive():
            os._exit(0)


if __name__ == "__main__":
    main()
