"""Load tunable scan settings. Frame numbers are never stored here."""

import json
from dataclasses import dataclass

from pageflow.errors import PageFlowError


@dataclass
class Settings:
    roi: object = None
    sample_fps: float = 5.0
    motion_threshold: float = 0.025
    motion_confirm_frames: int = 2
    stable_seconds: float = 0.8
    end_stable_seconds: float = 0.4
    detect_width: int = 192
    blur_ksize: int = 5
    block_rows: int = 6
    block_cols: int = 8
    dedup_mean_diff: float = 0.04
    dedup_block_diff: float = 0.05
    dedup_changed_fraction: float = 0.20
    min_sharpness_ratio: float = 0.5
    jpeg_quality: int = 95

    def to_dict(self):
        return {
            "roi": None
            if self.roi is None
            else {
                "x": self.roi[0],
                "y": self.roi[1],
                "width": self.roi[2],
                "height": self.roi[3],
            },
            "sample_fps": self.sample_fps,
            "motion_threshold": self.motion_threshold,
            "motion_confirm_frames": self.motion_confirm_frames,
            "stable_seconds": self.stable_seconds,
            "end_stable_seconds": self.end_stable_seconds,
            "detect_width": self.detect_width,
            "blur_ksize": self.blur_ksize,
            "block_rows": self.block_rows,
            "block_cols": self.block_cols,
            "dedup_mean_diff": self.dedup_mean_diff,
            "dedup_block_diff": self.dedup_block_diff,
            "dedup_changed_fraction": self.dedup_changed_fraction,
            "min_sharpness_ratio": self.min_sharpness_ratio,
            "jpeg_quality": self.jpeg_quality,
        }


_FIELDS = set(Settings().to_dict())


def require_roi(roi, width, height):
    x, y, roi_width, roi_height = roi
    if (
        x < 0
        or y < 0
        or roi_width < 1
        or roi_height < 1
        or x + roi_width > width
        or y + roi_height > height
    ):
        raise PageFlowError(
            f"扫描区域 ({x}, {y}, {roi_width}, {roi_height}) 超出画面 {width}x{height}"
        )
    return x, y, roi_width, roi_height


def settings_from_dict(data):
    if not isinstance(data, dict):
        raise PageFlowError("清单中的 settings 必须是对象")
    current = Settings().to_dict()
    current.update(data)
    return _settings_from_mapping(current, "清单中的 settings")


def load_settings(path):
    if path is None:
        return Settings()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PageFlowError(f"找不到配置文件: {path}") from exc
    except OSError as exc:
        raise PageFlowError(f"无法读取配置文件: {path} ({exc})") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PageFlowError(f"配置文件不是合法 JSON: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise PageFlowError(f"配置文件的顶层必须是对象: {path}")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise PageFlowError("配置里有无法识别的字段: " + ", ".join(unknown))
    current = Settings().to_dict()
    current.update(data)
    return _settings_from_mapping(current, path)


def _settings_from_mapping(data, path):
    roi = _parse_roi(data.get("roi"), path)
    try:
        settings = Settings(
            roi=roi,
            sample_fps=_positive_number(data, "sample_fps", path),
            motion_threshold=_unit_interval(data, "motion_threshold", path),
            motion_confirm_frames=_positive_int(data, "motion_confirm_frames", path),
            stable_seconds=_non_negative_number(data, "stable_seconds", path),
            end_stable_seconds=_non_negative_number(data, "end_stable_seconds", path),
            detect_width=_positive_int(data, "detect_width", path),
            blur_ksize=_odd_blur(data, path),
            block_rows=_positive_int(data, "block_rows", path),
            block_cols=_positive_int(data, "block_cols", path),
            dedup_mean_diff=_unit_interval(data, "dedup_mean_diff", path),
            dedup_block_diff=_unit_interval(data, "dedup_block_diff", path),
            dedup_changed_fraction=_unit_interval(data, "dedup_changed_fraction", path),
            min_sharpness_ratio=_unit_interval(data, "min_sharpness_ratio", path),
            jpeg_quality=_jpeg_quality(data, path),
        )
    except TypeError as exc:
        raise PageFlowError(f"配置字段类型不正确: {path} ({exc})") from exc
    return settings


def _parse_roi(value, path):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PageFlowError(f"roi 必须是对象或 null: {path}")
    expected = {"x", "y", "width", "height"}
    if set(value) != expected:
        raise PageFlowError("roi 必须包含且仅包含 x、y、width、height")
    try:
        roi = tuple(int(value[key]) for key in ("x", "y", "width", "height"))
    except (TypeError, ValueError) as exc:
        raise PageFlowError(f"roi 的坐标和尺寸必须是整数: {path}") from exc
    if roi[2] < 1 or roi[3] < 1:
        raise PageFlowError("roi 的 width 和 height 必须大于 0")
    return roi


def _number(data, name, path):
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PageFlowError(f"{name} 必须是数字: {path}")
    return value


def _positive_number(data, name, path):
    value = _number(data, name, path)
    if value <= 0:
        raise PageFlowError(f"{name} 必须大于 0")
    return float(value)


def _non_negative_number(data, name, path):
    value = _number(data, name, path)
    if value < 0:
        raise PageFlowError(f"{name} 不能为负")
    return float(value)


def _unit_interval(data, name, path):
    value = _number(data, name, path)
    if value < 0 or value > 1:
        raise PageFlowError(f"{name} 必须在 0 到 1 之间")
    return float(value)


def _positive_int(data, name, path):
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PageFlowError(f"{name} 必须是正整数")
    if value < 1:
        raise PageFlowError(f"{name} 必须是正整数")
    return value


def _odd_blur(data, path):
    value = _positive_int(data, "blur_ksize", path)
    if value % 2 == 0:
        raise PageFlowError("blur_ksize 必须是奇数")
    return value


def _jpeg_quality(data, path):
    value = data["jpeg_quality"]
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise PageFlowError(f"jpeg_quality 必须是 1 到 100 的整数: {path}")
    return value
