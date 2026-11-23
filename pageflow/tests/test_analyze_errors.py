import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class AnalyzeErrorTests(unittest.TestCase):
    def setUp(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("需要 OpenCV")
        self.cv2 = cv2
        from pageflow.analyze import analyze_video
        from pageflow.config import Settings

        self.analyze_video = analyze_video
        self.settings = Settings(roi=None, sample_fps=5)

    def test_missing_video_is_reported(self):
        from pageflow.errors import PageFlowError

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.mp4"
            with self.assertRaises(PageFlowError) as caught:
                self.analyze_video(missing, Path(directory) / "out", self.settings)
            self.assertIn("找不到视频", str(caught.exception))

    def test_unreadable_video_is_reported(self):
        from pageflow.errors import PageFlowError

        with tempfile.TemporaryDirectory() as directory:
            bogus = Path(directory) / "notes.txt"
            bogus.write_text("this is not a video", encoding="utf-8")
            with self.assertRaises(PageFlowError) as caught:
                self.analyze_video(bogus, Path(directory) / "out", self.settings)
            message = str(caught.exception)
            self.assertTrue("无法打开视频" in message or "没有有效帧" in message)

    def test_output_file_is_not_used_as_a_directory(self):
        from pageflow.errors import PageFlowError

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "empty.mp4"
            video.write_bytes(b"")
            output = Path(directory) / "blocked"
            output.write_text("file", encoding="utf-8")
            with self.assertRaises(PageFlowError) as caught:
                self.analyze_video(video, output, self.settings)
            message = str(caught.exception)
            self.assertTrue("不是目录" in message or "无法打开视频" in message or "没有有效帧" in message)


if __name__ == "__main__":
    unittest.main()
