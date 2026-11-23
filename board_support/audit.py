#!/usr/bin/env python3
"""audit.py — 独立翻页事件审计器(不漏页三保险的旁路)

对原始视频独立数一遍"运动事件"(burst),对照 PageFlow manifest 的页数:
事件数 ≠ (页数-1) 就发旗标,逼进人工/确认层复核。宁可虚报,不可漏页。

职责边界(2026-11-04 定,有实测依据):
- 本工具只做:事件计数、与 manifest 页数对账、页帧是否落在安静窗校验。
- 本工具不做:同页/异页判定。像素差分判书页身份已被证伪——
  fixtures/rec_20261103_220825 验收:同页对(手遮挡+曝光漂移)diff 21~22
  反而高于异页对 16~18。内容身份只能靠 OCR 文本确认层(confirm.py)。

用法:
  python3 audit.py --video rec.mp4 --manifest manifest.json [-o audit.json]
      [--sample-fps 5] [--motion-th 0.025] [--merge-gap 1.0] [--width 192]
"""
import argparse
import json
import sys

import cv2
import numpy as np


def sample_motion(video, sample_fps, width):
    """按 sample_fps 抽帧,算相邻帧 ROI 灰度差分均值。返回 [(ts, motion)]。"""
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        sys.exit(f"[audit] 打不开视频: {video}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 20.0
    step = max(1, round(src_fps / sample_fps))
    prev = None
    out = []
    while True:
        ok = cap.grab()
        if not ok:
            break
        idx = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
        if idx % step != 0:
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (width, int(width * gray.shape[0] / gray.shape[1])))
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        ts = idx / src_fps
        if prev is not None:
            # 归一化到 0~1,与 PageFlow metrics.csv 同尺度(threshold 0.025 口径)
            out.append((ts, float(np.mean(cv2.absdiff(gray, prev))) / 255.0))
        prev = gray
    cap.release()
    return out


def find_bursts(motions, th, confirm_n):
    """连续 confirm_n 个采样超阈 → burst。返回 [(start, end)]。"""
    bursts = []
    n_over = 0
    start = None
    for ts, m in motions:
        if m > th:
            n_over += 1
            if n_over == 1:
                start = ts
        else:
            if n_over >= confirm_n:
                bursts.append((start, ts))
            n_over = 0
            start = None
    if n_over >= confirm_n:
        bursts.append((start, motions[-1][0]))
    return bursts


def merge_bursts(bursts, gap_s):
    """间隔 < gap_s 的 burst 并为一个事件(连翻/手部二次动作)。"""
    if not bursts:
        return []
    merged = [list(bursts[0])]
    for s, e in bursts[1:]:
        if s - merged[-1][1] < gap_s:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [tuple(x) for x in merged]


def quiet_at(motions, ts, th):
    """ts 时刻前后各一个采样窗内 motion 是否低于阈。"""
    lo = ts - 0.2
    hi = ts + 0.2
    vals = [m for t, m in motions if lo <= t <= hi]
    if not vals:  # 视频边缘,取最近采样
        vals = [min(motions, key=lambda tm: abs(tm[0] - ts))[1]]
    return max(vals) <= th * 1.6  # 安静窗放宽一点


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("-o", "--out", default="audit.json")
    ap.add_argument("--sample-fps", type=float, default=5.0)
    ap.add_argument("--motion-th", type=float, default=0.025)
    ap.add_argument("--confirm", type=int, default=2, help="连续N个采样超阈才算burst")
    ap.add_argument("--merge-gap", type=float, default=1.0, help="burst间隔小于此秒数并为一个事件")
    ap.add_argument("--width", type=int, default=192)
    args = ap.parse_args()

    motions = sample_motion(args.video, args.sample_fps, args.width)
    if len(motions) < 4:
        sys.exit("[audit] 采样帧太少,视频太短或解码失败")
    raw_bursts = find_bursts(motions, args.motion_th, args.confirm)
    events = merge_bursts(raw_bursts, args.merge_gap)

    with open(args.manifest) as f:
        manifest = json.load(f)
    pages = [p for p in manifest.get("pages", []) if p.get("include", True)]
    boundaries = max(0, len(pages) - 1)

    flags = []
    # 1) 事件数对账
    if len(events) != boundaries:
        flags.append({
            "type": "count_mismatch",
            "detail": f"运动事件 {len(events)} 个 vs 翻页边界 {boundaries} 个"
                      f"(页数 {len(pages)});多出的事件可能是丢页或手部动作,"
                      f"少则可能过分割。须逐个对时序复核。",
            "events": [list(e) for e in events],
        })
    # 2) 页帧必须落在安静窗
    for p in pages:
        if not quiet_at(motions, p["timestamp"], args.motion_th):
            flags.append({
                "type": "page_in_motion",
                "detail": f"page order={p.get('order')} t={p['timestamp']}"
                          f" 附近仍有运动,选帧可能取在翻页中间态",
            })
    # 3) 结尾事件特别提示(视频截断的翻页最危险)
    if events and motions and events[-1][1] >= motions[-1][0] - 0.5:
        flags.append({
            "type": "burst_at_eof",
            "detail": f"视频结尾事件 [{events[-1][0]:.1f}s, EOF]:若为翻页则该页"
                      f"因无安静窗不会被收录;若为收尾手部动作则无害。需内容复核。",
        })

    result = {
        "video": args.video,
        "manifest": args.manifest,
        "params": {"sample_fps": args.sample_fps, "motion_th": args.motion_th,
                   "confirm": args.confirm, "merge_gap": args.merge_gap},
        "n_motion_events": len(events),
        "events": [list(e) for e in events],
        "n_pages": len(pages),
        "n_boundaries": boundaries,
        "flags": flags,
        "verdict": "PASS" if not flags else "REVIEW",
    }
    with open(args.out, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[audit] 事件 {len(events)} vs 边界 {boundaries} | 旗标 {len(flags)} | {result['verdict']}")
    for fl in flags:
        print(f"  - {fl['type']}: {fl['detail'][:90]}")


if __name__ == "__main__":
    main()
