import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pageflow.errors import PageFlowError
from pageflow.pdf_export import export_pages, jpeg_info, write_image_pdf

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class PdfExportTests(unittest.TestCase):
    def setUp(self):
        self.red = (FIXTURES / "red.jpg").read_bytes()
        self.blue = (FIXTURES / "blue.jpg").read_bytes()
        self.green = (FIXTURES / "green.jpg").read_bytes()

    def test_jpeg_dimensions(self):
        self.assertEqual(jpeg_info(self.red)[:2], (8, 8))

    def test_pdf_embeds_original_jpeg_bytes_in_order(self):
        destination = FIXTURES / "_two.pdf"
        try:
            write_image_pdf([self.red, self.blue], destination)
            data = destination.read_bytes()
        finally:
            if destination.exists():
                destination.unlink()
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertIn(b"%%EOF", data)
        self.assertLess(data.index(self.red), data.index(self.blue))
        self.assertEqual(data.count(b"/Type /Page "), 2)
        self.assertIn(b"/Filter /DCTDecode", data)

    def test_empty_export_explains_that_no_page_is_selected(self):
        with self.assertRaises(PageFlowError) as caught:
            write_image_pdf([], FIXTURES / "unused.pdf")
        self.assertIn("没有可导出的页面", str(caught.exception))

    def test_non_jpeg_is_rejected(self):
        with self.assertRaises(PageFlowError):
            jpeg_info(b"not a jpeg")

    def test_manifest_export_skips_excluded_pages_and_keeps_time_order(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            files = {
                "frames/crop/early.jpg": self.blue,
                "frames/crop/middle.jpg": self.green,
                "frames/crop/late.jpg": self.red,
            }
            for relative, blob in files.items():
                path = output / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob)
            pages = [
                self._page(30, 3.0, "frames/crop/late.jpg", include=True),
                self._page(20, 2.0, "frames/crop/middle.jpg", include=False),
                self._page(10, 1.0, "frames/crop/early.jpg", include=True),
            ]
            pdf_path = output / "pages.pdf"
            count = export_pages(output / "manifest.json", pages, output, pdf_path)
            data = pdf_path.read_bytes()
            self.assertEqual(count, 2)
            self.assertLess(data.index(self.blue), data.index(self.red))
            self.assertNotIn(self.green, data)
            by_frame = {page["frame_index"]: page["pdf_page"] for page in pages}
            self.assertEqual(by_frame, {10: 1, 20: None, 30: 2})

    def _page(self, frame_index, timestamp, relative, include):
        return {
            "frame_index": frame_index,
            "timestamp": timestamp,
            "include": include,
            "crop_image": relative,
            "full_image": relative,
            "pdf_page": None,
        }


if __name__ == "__main__":
    unittest.main()
