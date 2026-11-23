"""Command line for offline page capture."""

import argparse
import sys
from pathlib import Path

from pageflow.analyze import add_manual_frame, analyze_video, export_manifest, set_page_included
from pageflow.config import load_settings
from pageflow.errors import PageFlowError
from pageflow.manifest import included_pages


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except PageFlowError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def _parser():
    parser = argparse.ArgumentParser(
        prog="pageflow",
        description="从翻页视频中选取稳定的书籍跨页，并导出图片型 PDF。",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="分析视频，写出页面图片、清单和 PDF")
    analyze.add_argument("--video", required=True, help="输入视频路径，不会被修改")
    analyze.add_argument("--output", required=True, help="输出目录")
    analyze.add_argument("--config", help="JSON 配置。省略时使用内置默认值，扫描区域为整幅画面")
    analyze.add_argument("--force", action="store_true", help="覆盖输出目录里已有的清单和生成图片")
    analyze.add_argument("--no-pdf", action="store_true", help="只写图片和清单，不导出 PDF")

    export = commands.add_parser("export", help="按清单中 include 为 true 的页面重新导出 PDF")
    export.add_argument("--manifest", required=True, help="manifest.json 路径")
    export.add_argument("--pdf", help="PDF 输出路径，默认写到清单同目录的 pages.pdf")
    export.add_argument("--full", action="store_true", help="使用未裁剪的整帧，默认使用扫描区域裁剪图")

    add_frame = commands.add_parser("add-frame", help="按帧号或时间戳补选一页")
    add_frame.add_argument("--manifest", required=True)
    add_frame.add_argument("--video", help="原始视频。清单里的路径不可用时必须提供")
    choice = add_frame.add_mutually_exclusive_group(required=True)
    choice.add_argument("--frame", type=int, help="从 0 开始的帧号")
    choice.add_argument("--timestamp", type=float, help="秒。会换算成最接近的帧")

    exclude = commands.add_parser("exclude", help="把一页标为不进入 PDF")
    _select_page(exclude)
    include = commands.add_parser("include", help="把已排除的一页重新纳入 PDF")
    _select_page(include)
    return parser


def _select_page(parser):
    parser.add_argument("--manifest", required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--order", type=int, help="清单中的 order")
    choice.add_argument("--frame", type=int, help="帧号")


def _dispatch(args):
    if args.command == "analyze":
        return _analyze(args)
    if args.command == "export":
        return _export(args)
    if args.command == "add-frame":
        return _add_frame(args)
    if args.command == "exclude":
        return _mark(args, include=False)
    if args.command == "include":
        return _mark(args, include=True)
    raise PageFlowError(f"未知命令: {args.command}")


def _analyze(args):
    config_path = Path(args.config) if args.config else None
    settings = load_settings(config_path)
    manifest, pdf_path = analyze_video(
        args.video,
        args.output,
        settings,
        force=args.force,
        write_pdf=not args.no_pdf,
    )
    info = manifest["video_info"]
    print(
        f"视频 {info['width']}x{info['height']}，{info['fps']:.3f} fps，读取 {info['frames_read']} 帧，"
        f"每 {info['sample_step']} 帧采样一次，共 {info['samples']} 个采样。"
    )
    print(f"清单: {Path(args.output) / 'manifest.json'}")
    _print_pages(manifest["pages"])
    rejected = manifest.get("rejected_duplicates") or []
    if rejected:
        print(f"已合并重复跨页 {len(rejected)} 次。")
    if not manifest["pages"]:
        print(
            "没有检测到足够长的稳定画面。可以减小 stable_seconds 或 motion_threshold，"
            "或用 add-frame 按帧号补选。",
            file=sys.stderr,
        )
        return 2
    if pdf_path is not None:
        print(f"PDF: {pdf_path}，{len(included_pages(manifest['pages']))} 页")
    elif args.no_pdf:
        print("未导出 PDF。")
    return 0


def _export(args):
    pdf_path, count = export_manifest(args.manifest, pdf_path=args.pdf, use_full=args.full)
    print(f"PDF: {pdf_path}，{count} 页")
    return 0


def _add_frame(args):
    page, created = add_manual_frame(
        args.manifest,
        video_path=args.video,
        frame_index=args.frame,
        timestamp=args.timestamp,
    )
    action = "已补选" if created else "该帧已在清单中，已重新纳入"
    print(
        f"{action}: order {page['order']}，帧 {page['frame_index']}，"
        f"时间 {page['timestamp']:.3f} 秒。重新导出请执行 export。"
    )
    return 0


def _mark(args, include):
    page = set_page_included(
        args.manifest,
        include,
        order=args.order,
        frame_index=args.frame,
    )
    state = "纳入" if include else "排除"
    print(f"已{state}: order {page['order']}，帧 {page['frame_index']}。重新导出请执行 export。")
    return 0


def _print_pages(pages):
    if not pages:
        print("没有选中页面。")
        return
    print("order  include  time      frame   reason         review")
    for page in pages:
        review = "yes" if page.get("review") else "no"
        print(
            f"{page['order']:5d}  {str(page['include']):7}  "
            f"{page['timestamp']:8.3f}  {page['frame_index']:6d}  "
            f"{page['reason']:<13}  {review}"
        )


if __name__ == "__main__":
    sys.exit(main())
