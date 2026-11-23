import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pageflow.dedup import image_difference, is_same_page


class DedupRuleTests(unittest.TestCase):
    def test_same_page_requires_both_measures_to_be_below_limits(self):
        self.assertTrue(is_same_page(0.01, 0.05, 0.04, 0.20))

    def test_many_changed_blocks_keep_a_similar_looking_page(self):
        self.assertFalse(is_same_page(0.02, 0.45, 0.04, 0.20))

    def test_large_average_change_is_not_a_duplicate(self):
        self.assertFalse(is_same_page(0.12, 0.10, 0.04, 0.20))

    def test_value_equal_to_a_limit_is_kept(self):
        self.assertFalse(is_same_page(0.04, 0.05, 0.04, 0.20))
        self.assertFalse(is_same_page(0.01, 0.20, 0.04, 0.20))


class ImageDifferenceTests(unittest.TestCase):
    def test_identical_images_have_no_difference(self):
        image = [[0, 0, 10, 10], [0, 0, 10, 10], [5, 5, 5, 5], [5, 5, 5, 5]]
        mean_diff, changed = image_difference(image, image, 2, 2, 0.05)
        self.assertEqual(mean_diff, 0)
        self.assertEqual(changed, 0)

    def test_one_block_change_does_not_mark_the_whole_image(self):
        left = [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
        right = [[255, 255, 0, 0], [255, 255, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
        mean_diff, changed = image_difference(left, right, 2, 2, 0.05)
        self.assertAlmostEqual(mean_diff, 0.25)
        self.assertAlmostEqual(changed, 0.25)

    def test_full_change_marks_every_block(self):
        left = [[0, 0], [0, 0]]
        right = [[255, 255], [255, 255]]
        mean_diff, changed = image_difference(left, right, 1, 1, 0.05)
        self.assertAlmostEqual(mean_diff, 1)
        self.assertAlmostEqual(changed, 1)


if __name__ == "__main__":
    unittest.main()
