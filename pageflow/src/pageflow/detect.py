"""Read a video and measure motion on a downscaled scan area.

Saved images are encoded from the original frame. The downscaled image is
only a signature for motion and duplicate checks.
"""

import cv2

from pageflow.config import require_roi
from pageflow.errors import PageFlowError
from pageflow.state_machine import Observation


def collect_samples(video_path, settings):
    capture = _open_video(video_path)
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    reported_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if fps <= 0 or width < 1 or height < 1:
        capture.release()
        raise PageFlowError(f"视频缺少有效的帧率或分辨率: {video_path}")
    roi = settings.roi if settings.roi is not None else (0, 0, width, height)
    roi = require_roi(roi, width, height)
    step = max(1, int(round(fps / settings.sample_fps)))
    observations = []
    encoded_frames = {}
    previous = None
    frame_index = 0
    valid_frames = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            valid_frames += 1
            if frame_index % step == 0:
                observation, encoded, previous = _measure_frame(
                    frame, frame_index, fps, roi, previous, settings
                )
                observations.append(observation)
                encoded_frames[frame_index] = encoded
            frame_index += 1
    finally:
        capture.release()
    if valid_frames == 0:
        raise PageFlowError(f"视频中没有有效帧: {video_path}")
    info = {
        "width": width,
        "height": height,
        "fps": fps,
        "reported_frames": reported_frames,
        "frames_read": valid_frames,
        "sample_step": step,
        "samples": len(observations),
        "roi": {"x": roi[0], "y": roi[1], "width": roi[2], "height": roi[3]},
    }
    return observations, encoded_frames, info


def read_frame(video_path, frame_index):
    if frame_index < 0:
        raise PageFlowError(f"帧号不能为负: {frame_index}")
    capture = _open_video(video_path)
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    index = 0
    target = None
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            if index == frame_index:
                target = frame
                break
            index += 1
    finally:
        capture.release()
    if target is None:
        raise PageFlowError(f"视频中没有帧号 {frame_index}: {video_path}")
    if fps <= 0 or width < 1 or height < 1:
        raise PageFlowError(f"视频缺少有效的帧率或分辨率: {video_path}")
    return target, fps, width, height


def encode_frame(frame, roi, jpeg_quality):
    x, y, width, height = roi
    crop = frame[y : y + height, x : x + width]
    if crop.size == 0:
        raise PageFlowError("扫描区域为空，无法保存页面")
    return _encode_jpeg(frame, jpeg_quality), _encode_jpeg(crop, jpeg_quality)


def _open_video(video_path):
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise PageFlowError(f"无法打开视频: {video_path}")
    return capture


def _measure_frame(frame, frame_index, fps, roi, previous, settings):
    x, y, width, height = roi
    crop = frame[y : y + height, x : x + width]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    detect_height = max(1, int(round(settings.detect_width * height / float(width))))
    small = cv2.resize(gray, (settings.detect_width, detect_height), interpolation=cv2.INTER_AREA)
    if settings.blur_ksize >= 3:
        small = cv2.GaussianBlur(small, (settings.blur_ksize, settings.blur_ksize), 0)
    if previous is None:
        motion = 0.0
    else:
        motion = float(cv2.absdiff(previous, small).mean()) / 255.0
    sharpness = float(cv2.Laplacian(cv2.GaussianBlur(gray, (3, 3), 0), cv2.CV_64F).var())
    encoded = encode_frame(frame, roi, settings.jpeg_quality)
    observation = Observation(
        frame_index=frame_index,
        timestamp=frame_index / fps,
        motion=motion,
        sharpness=sharpness,
        signature=small,
    )
    return observation, encoded, small


def _encode_jpeg(image, quality):
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise PageFlowError("无法把画面编码为 JPEG")
    return encoded.tobytes()
