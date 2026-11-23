"""Analyze a recording, write the review manifest, and export a PDF."""

import csv
from datetime import datetime, timezone
from pathlib import Path

from pageflow.config import require_roi, settings_from_dict
from pageflow.dedup import image_difference, is_same_page
from pageflow.detect import collect_samples, encode_frame, read_frame
from pageflow.errors import PageFlowError
from pageflow.manifest import (
    MANIFEST_NAME,
    assign_order,
    clear_generated_images,
    insert_page,
    load_manifest,
    prepare_output_dir,
    save_manifest,
    set_included,
)
from pageflow.pdf_export import export_pages
from pageflow.state_machine import PageSelector

MANUAL_NOTE = (
    "画面稳定只表示采样区域变化变小，不表示没有手遮挡。"
    "请查看 frames/crop 里的图片。遮挡、模糊或重复的页面把 include 改为 false；"
    "漏掉的页面用 add-frame 按帧号或时间戳补选，然后重新执行 export。"
)


def analyze_video(video_path, output_dir, settings, force=False, write_pdf=True):
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    if not video_path.is_file():
        raise PageFlowError(f"找不到视频: {video_path}")
    prepare_output_dir(output_dir, force=force)
    size_before = video_path.stat().st_size
    observations, encoded_frames, video_info = collect_samples(video_path, settings)
    if video_path.stat().st_size != size_before:
        raise PageFlowError(f"分析过程中原始视频大小发生变化，已中止: {video_path}")

    selector = PageSelector(
        stable_seconds=settings.stable_seconds,
        motion_threshold=settings.motion_threshold,
        motion_confirm_frames=settings.motion_confirm_frames,
        end_stable_seconds=settings.end_stable_seconds,
        is_duplicate=lambda left, right: _same_signature(left, right, settings),
    )
    selections = selector.select(observations)
    pages = _pages_from_selections(selections, settings)
    manifest = {
        "version": 1,
        "video": str(video_path),
        "video_resolved": str(video_path.resolve()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings.to_dict(),
        "video_info": video_info,
        "notes": MANUAL_NOTE,
        "pages": pages,
        "rejected_duplicates": [
            {
                "frame_index": item.frame_index,
                "timestamp": round(item.timestamp, 4),
                "sharpness": round(item.sharpness, 3),
                "reason": "duplicate",
            }
            for item in selector.rejections
        ],
    }
    clear_generated_images(output_dir)
    _write_page_images(output_dir, pages, encoded_frames)
    _write_metrics(output_dir / "metrics.csv", observations)
    manifest_path = output_dir / MANIFEST_NAME
    save_manifest(manifest_path, manifest)
    pdf_path = None
    if write_pdf and pages:
        pdf_path = output_dir / "pages.pdf"
        export_pages(manifest_path, manifest["pages"], output_dir, pdf_path, use_full=False)
        save_manifest(manifest_path, manifest)
    return manifest, pdf_path


def export_manifest(manifest_path, pdf_path=None, use_full=False):
    manifest_path = Path(manifest_path)
    manifest = load_manifest(manifest_path)
    output_dir = manifest_path.parent
    destination = Path(pdf_path) if pdf_path else output_dir / "pages.pdf"
    count = export_pages(manifest_path, manifest["pages"], output_dir, destination, use_full=use_full)
    save_manifest(manifest_path, manifest)
    return destination, count


def set_page_included(manifest_path, include, order=None, frame_index=None):
    manifest_path = Path(manifest_path)
    manifest = load_manifest(manifest_path)
    page = set_included(manifest["pages"], include, order=order, frame_index=frame_index)
    save_manifest(manifest_path, manifest)
    return page


def add_manual_frame(manifest_path, video_path=None, frame_index=None, timestamp=None):
    if frame_index is None and timestamp is None:
        raise PageFlowError("补选页面时需要提供 --frame 或 --timestamp")
    manifest_path = Path(manifest_path)
    manifest = load_manifest(manifest_path)
    video = Path(video_path or manifest.get("video") or "")
    if not video.is_file():
        raise PageFlowError(f"找不到视频: {video}。请用 --video 指定原始视频。")
    settings = settings_from_dict(manifest.get("settings") or {})
    if frame_index is None:
        probe, fps, _, _ = read_frame(video, 0)
        del probe
        frame_index = int(round(float(timestamp) * fps))
    size_before = video.stat().st_size
    frame, fps, width, height = read_frame(video, frame_index)
    if video.stat().st_size != size_before:
        raise PageFlowError(f"读取帧时原始视频大小发生变化，已中止: {video}")
    roi = settings.roi if settings.roi is not None else (0, 0, width, height)
    roi = require_roi(roi, width, height)
    full_bytes, crop_bytes = encode_frame(frame, roi, settings.jpeg_quality)
    output_dir = manifest_path.parent
    full_relative = f"frames/full/f{frame_index:06d}.jpg"
    crop_relative = f"frames/crop/f{frame_index:06d}.jpg"
    _write_bytes(output_dir / full_relative, full_bytes)
    _write_bytes(output_dir / crop_relative, crop_bytes)
    page = {
        "order": 0,
        "include": True,
        "frame_index": frame_index,
        "timestamp": round(frame_index / fps, 4),
        "sharpness": None,
        "motion": None,
        "reason": "manual",
        "source": "manual",
        "full_image": full_relative,
        "crop_image": crop_relative,
        "mean_diff_from_previous": None,
        "changed_fraction_from_previous": None,
        "review": True,
        "review_reasons": ["手动补选，需要人工确认"],
        "pdf_page": None,
    }
    saved, created = insert_page(manifest["pages"], page)
    if not created:
        saved["full_image"] = full_relative
        saved["crop_image"] = crop_relative
        saved["include"] = True
        assign_order(manifest["pages"])
    save_manifest(manifest_path, manifest)
    return saved, created


def _same_signature(left, right, settings):
    try:
        mean_diff, changed_fraction = image_difference(
            left,
            right,
            settings.block_rows,
            settings.block_cols,
            settings.dedup_block_diff,
        )
    except ValueError as exc:
        raise PageFlowError(str(exc)) from exc
    return is_same_page(
        mean_diff,
        changed_fraction,
        settings.dedup_mean_diff,
        settings.dedup_changed_fraction,
    )


def _pages_from_selections(selections, settings):
    previous = None
    pages = []
    for selection in selections:
        mean_diff = None
        changed_fraction = None
        if previous is not None:
            mean_diff, changed_fraction = image_difference(
                previous,
                selection.signature,
                settings.block_rows,
                settings.block_cols,
                settings.dedup_block_diff,
            )
            mean_diff = round(mean_diff, 6)
            changed_fraction = round(changed_fraction, 6)
        pages.append(
            {
                "order": 0,
                "include": True,
                "frame_index": selection.frame_index,
                "timestamp": round(selection.timestamp, 4),
                "sharpness": round(selection.sharpness, 3),
                "motion": round(selection.motion, 6),
                "reason": selection.reason,
                "source": "auto",
                "full_image": f"frames/full/f{selection.frame_index:06d}.jpg",
                "crop_image": f"frames/crop/f{selection.frame_index:06d}.jpg",
                "mean_diff_from_previous": mean_diff,
                "changed_fraction_from_previous": changed_fraction,
                "review": False,
                "review_reasons": [],
                "pdf_page": None,
            }
        )
        previous = selection.signature
    _mark_soft_frames(pages, settings.min_sharpness_ratio)
    return assign_order(pages)


def _mark_soft_frames(pages, ratio):
    scores = [page["sharpness"] for page in pages if isinstance(page["sharpness"], (int, float))]
    if ratio <= 0 or len(scores) < 2:
        return
    ordered = sorted(scores)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        median = ordered[middle]
    else:
        median = (ordered[middle - 1] + ordered[middle]) / 2
    if median <= 0:
        return
    for page in pages:
        if page["sharpness"] < ratio * median:
            page["review"] = True
            page["review_reasons"].append("清晰度明显低于本次其他页面")


def _write_page_images(output_dir, pages, encoded_frames):
    for page in pages:
        encoded = encoded_frames.get(page["frame_index"])
        if encoded is None:
            raise PageFlowError(f"内部错误：没有第 {page['frame_index']} 帧的图像数据")
        _write_bytes(output_dir / page["full_image"], encoded[0])
        _write_bytes(output_dir / page["crop_image"], encoded[1])


def _write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_metrics(path, observations):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frame_index", "timestamp", "motion", "sharpness"])
        for observation in observations:
            writer.writerow(
                [
                    observation.frame_index,
                    f"{observation.timestamp:.4f}",
                    f"{observation.motion:.6f}",
                    f"{observation.sharpness:.3f}",
                ]
            )
