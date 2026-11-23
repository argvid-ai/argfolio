"""Page list used both for review and for PDF export."""

import json
from pathlib import Path

from pageflow.errors import PageFlowError

MANIFEST_NAME = "manifest.json"


def prepare_output_dir(path, force):
    if path.exists() and not path.is_dir():
        raise PageFlowError(f"输出路径不是目录: {path}")
    manifest = path / MANIFEST_NAME
    if manifest.exists() and not force:
        raise PageFlowError(
            f"输出目录已有清单 {manifest}。自动分析会覆盖该清单；"
            "确认不需要保留现有修改后，加上 --force 再运行。"
        )
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise PageFlowError(f"输出目录不可写: {path} ({exc})") from exc


def clear_generated_images(path):
    for folder_name in ("full", "crop"):
        folder = path / "frames" / folder_name
        if not folder.exists():
            continue
        for item in folder.iterdir():
            if item.is_file():
                item.unlink()
    pdf_path = path / "pages.pdf"
    if pdf_path.exists():
        pdf_path.unlink()


def sort_pages(pages):
    return sorted(pages, key=lambda page: (page["timestamp"], page["frame_index"]))


def assign_order(pages):
    pages[:] = sort_pages(pages)
    for index, page in enumerate(pages, start=1):
        page["order"] = index
    return pages


def included_pages(pages):
    return [page for page in sort_pages(pages) if page.get("include", True)]


def load_manifest(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PageFlowError(f"找不到清单: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PageFlowError(f"清单不是合法 JSON: {path} ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
        raise PageFlowError(f"清单格式不正确，缺少 pages 数组: {path}")
    return data


def save_manifest(path, manifest):
    destination = Path(path)
    destination.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def find_page(pages, order=None, frame_index=None):
    matches = []
    for page in pages:
        if order is not None and page.get("order") == order:
            matches.append(page)
        elif frame_index is not None and page.get("frame_index") == frame_index:
            matches.append(page)
    if not matches:
        target = f"顺序 {order}" if order is not None else f"帧号 {frame_index}"
        raise PageFlowError(f"清单中没有{target}的页面")
    if len(matches) > 1:
        target = f"顺序 {order}" if order is not None else f"帧号 {frame_index}"
        raise PageFlowError(f"清单中有多条{target}的页面")
    return matches[0]


def set_included(pages, include, order=None, frame_index=None):
    page = find_page(pages, order=order, frame_index=frame_index)
    page["include"] = include
    return page


def insert_page(pages, page):
    for existing in pages:
        if existing["frame_index"] == page["frame_index"]:
            existing["include"] = True
            return existing, False
    pages.append(page)
    assign_order(pages)
    return page, True
