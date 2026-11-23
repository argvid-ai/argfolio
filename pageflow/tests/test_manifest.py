import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pageflow.config import Settings, load_settings
from pageflow.errors import PageFlowError
from pageflow.manifest import (
    assign_order,
    included_pages,
    insert_page,
    prepare_output_dir,
    set_included,
)


def page(frame_index, timestamp, include=True):
    return {
        "order": 0,
        "include": include,
        "frame_index": frame_index,
        "timestamp": timestamp,
        "full_image": f"frames/full/f{frame_index:06d}.jpg",
        "crop_image": f"frames/crop/f{frame_index:06d}.jpg",
    }


class ManifestOrderTests(unittest.TestCase):
    def test_pages_are_ordered_by_timestamp_then_frame(self):
        pages = [page(30, 3.0), page(10, 1.0), page(20, 2.0)]
        assign_order(pages)
        self.assertEqual([item["frame_index"] for item in pages], [10, 20, 30])
        self.assertEqual([item["order"] for item in pages], [1, 2, 3])

    def test_excluded_page_leaves_a_gap_out_of_the_pdf_sequence(self):
        pages = [page(10, 1.0), page(20, 2.0), page(30, 3.0)]
        assign_order(pages)
        set_included(pages, False, order=2)
        kept = included_pages(pages)
        self.assertEqual([item["frame_index"] for item in kept], [10, 30])
        self.assertEqual([item["order"] for item in pages], [1, 2, 3])

    def test_added_frame_is_inserted_by_time(self):
        pages = [page(10, 1.0), page(30, 3.0)]
        assign_order(pages)
        added, created = insert_page(pages, page(20, 2.0))
        self.assertTrue(created)
        self.assertEqual([item["frame_index"] for item in pages], [10, 20, 30])
        self.assertEqual(added["order"], 2)

    def test_adding_an_existing_frame_includes_it_again(self):
        pages = [page(10, 1.0, include=False)]
        assign_order(pages)
        saved, created = insert_page(pages, page(10, 1.0))
        self.assertFalse(created)
        self.assertTrue(saved["include"])
        self.assertEqual(len(pages), 1)


class OutputDirTests(unittest.TestCase):
    def test_existing_manifest_requires_force(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(PageFlowError) as caught:
                prepare_output_dir(output, force=False)
            self.assertIn("已有清单", str(caught.exception))
            prepare_output_dir(output, force=True)

    def test_file_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "not-a-directory"
            target.write_text("x", encoding="utf-8")
            with self.assertRaises(PageFlowError) as caught:
                prepare_output_dir(target, force=True)
            self.assertIn("不是目录", str(caught.exception))


class ConfigTests(unittest.TestCase):
    def test_default_file_matches_builtin_settings(self):
        path = Path(__file__).resolve().parents[1] / "config" / "default.json"
        loaded = load_settings(path)
        self.assertEqual(loaded.to_dict(), Settings().to_dict())

    def test_sample_roi_is_inside_the_recording(self):
        path = Path(__file__).resolve().parents[1] / "config" / "sample_rec_20261103.json"
        loaded = load_settings(path)
        x, y, width, height = loaded.roi
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + width, 1920)
        self.assertLessEqual(y + height, 1080)

    def test_unknown_field_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({"frame_number": 12}), encoding="utf-8")
            with self.assertRaises(PageFlowError) as caught:
                load_settings(path)
            self.assertIn("无法识别", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
