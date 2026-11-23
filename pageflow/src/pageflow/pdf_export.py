"""Pack JPEG pages into a PDF without decoding or recompressing them."""

from pageflow.errors import PageFlowError

_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3}


def jpeg_info(data):
    """Return width, height, and component count for a baseline or progressive JPEG."""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise PageFlowError("页面文件不是 JPEG，无法按原图打包 PDF")
    index = 2
    while index + 3 < len(data):
        if data[index] != 0xFF:
            raise PageFlowError("JPEG 结构无法解析，页面文件可能已损坏")
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if index + 1 >= len(data):
            break
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            raise PageFlowError("JPEG 结构无法解析，页面文件可能已损坏")
        if marker in _SOF_MARKERS:
            if segment_length < 8:
                raise PageFlowError("JPEG 图像尺寸不完整")
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            components = data[index + 7]
            if width < 1 or height < 1 or components not in (1, 3):
                raise PageFlowError(
                    f"不支持的 JPEG 尺寸或颜色通道: {width}x{height}，通道 {components}"
                )
            return width, height, components
        if marker == 0xDA:
            break
        index += segment_length
    raise PageFlowError("JPEG 中没有图像尺寸信息")


def write_image_pdf(jpeg_blobs, destination):
    """Write one PDF page per JPEG, embedding the original bytes."""
    if not jpeg_blobs:
        raise PageFlowError("没有可导出的页面。请在清单中至少保留一页 include 为 true 的图片。")
    pages = []
    for blob in jpeg_blobs:
        width, height, components = jpeg_info(blob)
        pages.append((blob, width, height, components))

    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
    }
    next_id = 3
    kids = []
    for blob, width, height, components in pages:
        page_id = next_id
        content_id = next_id + 1
        image_id = next_id + 2
        next_id += 3
        page_width = width * 72.0 / 150.0
        page_height = height * 72.0 / 150.0
        content = f"q {page_width:.4f} 0 0 {page_height:.4f} 0 0 cm /Im0 Do Q\n".encode("ascii")
        color_space = "/DeviceRGB" if components == 3 else "/DeviceGray"
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.4f} {page_height:.4f}] "
            f"/Contents {content_id} 0 R /Resources << /XObject << /Im0 {image_id} 0 R >> >> >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"endstream"
        )
        objects[image_id] = (
            (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace {color_space} /BitsPerComponent 8 /Filter /DCTDecode "
                f"/Length {len(blob)} >>\nstream\n"
            ).encode("ascii")
            + blob
            + b"\nendstream"
        )
        kids.append(page_id)

    kid_refs = " ".join(f"{kid} 0 R" for kid in kids)
    objects[2] = f"<< /Type /Pages /Count {len(pages)} /Kids [{kid_refs}] >>".encode("ascii")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * next_id
    for object_id in range(1, next_id):
        offsets[object_id] = len(output)
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref_at = len(output)
    output.extend(f"xref\n0 {next_id}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for object_id in range(1, next_id):
        output.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer << /Size {next_id} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n"
        ).encode("ascii")
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(output)


def export_pages(manifest_path, pages, output_dir, pdf_path, use_full=False):
    """Build a PDF from included pages and record the resulting PDF page numbers."""
    chosen = []
    blobs = []
    image_key = "full_image" if use_full else "crop_image"
    ordered = sorted(pages, key=lambda page: (page.get("timestamp", 0), page.get("frame_index", 0)))
    for page in ordered:
        page["pdf_page"] = None
        if not page.get("include", True):
            continue
        relative = page.get(image_key)
        if not relative:
            raise PageFlowError(f"页面缺少图片路径 {image_key}: 帧 {page.get('frame_index')}")
        image_path = output_dir / relative
        if not image_path.is_file():
            raise PageFlowError(f"找不到页面图片: {image_path}")
        chosen.append(page)
        blobs.append(image_path.read_bytes())
    write_image_pdf(blobs, pdf_path)
    for pdf_page, page in enumerate(chosen, start=1):
        page["pdf_page"] = pdf_page
    return len(chosen)
