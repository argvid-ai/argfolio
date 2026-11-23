import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pageflow.state_machine import Observation, PageSelector


def observation(frame_index, timestamp, motion, sharpness, signature):
    return Observation(frame_index, timestamp, motion, sharpness, signature)


def selector(is_duplicate=None, stable_seconds=0.8, end_stable_seconds=0.4, confirm=2, threshold=0.05):
    if is_duplicate is None:
        is_duplicate = lambda left, right: left == right
    return PageSelector(
        stable_seconds=stable_seconds,
        motion_threshold=threshold,
        motion_confirm_frames=confirm,
        end_stable_seconds=end_stable_seconds,
        is_duplicate=is_duplicate,
    )


def quiet_span(start, end, step, signature, sharpness=10, motion=0.0, frame_start=0):
    frames = []
    timestamp = start
    frame_index = frame_start
    while timestamp <= end + 1e-9:
        frames.append(observation(frame_index, timestamp, motion, sharpness, signature))
        timestamp += step
        frame_index += 1
    return frames


class StateMachineTests(unittest.TestCase):
    def test_single_page_is_saved_at_video_end_without_a_turn(self):
        frames = quiet_span(0.0, 2.0, 0.2, signature="A")
        frames[3] = observation(frames[3].frame_index, frames[3].timestamp, 0.0, 50, "A")
        chosen = selector().select(frames)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].reason, "video_end")
        self.assertEqual(chosen[0].signature, "A")
        self.assertEqual(chosen[0].sharpness, 50)
        self.assertEqual(chosen[0].frame_index, frames[3].frame_index)

    def test_last_page_does_not_need_a_following_turn(self):
        frames = []
        frames += quiet_span(0.0, 2.0, 0.2, "A", frame_start=0)
        frames += [
            observation(100, 2.2, 0.4, 1, "turn"),
            observation(101, 2.4, 0.4, 1, "turn"),
        ]
        frames += quiet_span(2.6, 4.6, 0.2, "B", frame_start=200)
        frames += [
            observation(300, 4.8, 0.4, 1, "turn"),
            observation(301, 5.0, 0.4, 1, "turn"),
        ]
        frames += quiet_span(5.2, 7.2, 0.2, "C", frame_start=400)
        chosen = selector().select(frames)
        self.assertEqual([item.signature for item in chosen], ["A", "B", "C"])
        self.assertEqual(
            [item.reason for item in chosen],
            ["turn_boundary", "turn_boundary", "video_end"],
        )
        self.assertLess(chosen[0].timestamp, chosen[1].timestamp)
        self.assertLess(chosen[1].timestamp, chosen[2].timestamp)

    def test_short_tail_after_a_turn_is_still_saved(self):
        frames = quiet_span(0.0, 2.0, 0.2, "A", frame_start=0)
        frames += [
            observation(50, 2.2, 0.5, 1, "turn"),
            observation(51, 2.4, 0.5, 1, "turn"),
            observation(52, 2.6, 0.0, 8, "B"),
            observation(53, 2.8, 0.0, 12, "B"),
            observation(54, 3.0, 0.0, 9, "B"),
        ]
        chosen = selector().select(frames)
        self.assertEqual([item.signature for item in chosen], ["A", "B"])
        self.assertEqual(chosen[1].reason, "video_end")
        self.assertEqual(chosen[1].frame_index, 53)

    def test_video_ending_during_motion_keeps_the_previous_page_only(self):
        frames = quiet_span(0.0, 2.0, 0.2, "A", frame_start=0)
        frames += [
            observation(80, 2.2, 0.8, 100, "blur"),
            observation(81, 2.4, 0.8, 100, "blur"),
            observation(82, 2.6, 0.8, 100, "blur"),
        ]
        chosen = selector().select(frames)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].signature, "A")
        self.assertNotEqual(chosen[0].frame_index, 80)

    def test_single_motion_spike_does_not_split_a_page(self):
        frames = quiet_span(0.0, 1.0, 0.2, "A", frame_start=0)
        frames.append(observation(99, 1.2, 0.9, 1, "noise"))
        frames += quiet_span(1.4, 3.0, 0.2, "A", frame_start=120)
        chosen = selector().select(frames)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].signature, "A")

    def test_duplicate_after_a_false_turn_is_not_added(self):
        frames = quiet_span(0.0, 2.0, 0.2, "A", sharpness=5, frame_start=0)
        frames += [
            observation(40, 2.2, 0.5, 1, "turn"),
            observation(41, 2.4, 0.5, 1, "turn"),
        ]
        frames += quiet_span(2.6, 4.6, 0.2, "A", sharpness=4, frame_start=60)
        machine = selector()
        chosen = machine.select(frames)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(len(machine.rejections), 1)
        self.assertEqual(chosen[0].sharpness, 5)

    def test_sharper_duplicate_replaces_the_saved_frame(self):
        frames = quiet_span(0.0, 2.0, 0.2, "A", sharpness=5, frame_start=0)
        frames += [
            observation(40, 2.2, 0.5, 1, "turn"),
            observation(41, 2.4, 0.5, 1, "turn"),
        ]
        frames += quiet_span(2.6, 4.4, 0.2, "A", sharpness=4, frame_start=60)
        frames.append(observation(90, 4.6, 0.0, 30, "A"))
        chosen = selector().select(frames)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].frame_index, 90)
        self.assertEqual(chosen[0].sharpness, 30)

    def test_different_pages_with_distinct_signatures_are_both_kept(self):
        frames = quiet_span(0.0, 2.0, 0.2, "layout-a", frame_start=0)
        frames += [
            observation(30, 2.2, 0.6, 1, "turn"),
            observation(31, 2.4, 0.6, 1, "turn"),
        ]
        frames += quiet_span(2.6, 4.6, 0.2, "layout-b", frame_start=50)
        chosen = selector(is_duplicate=lambda left, right: False).select(frames)
        self.assertEqual([item.signature for item in chosen], ["layout-a", "layout-b"])

    def test_page_shown_shorter_than_stable_seconds_is_not_emitted_at_a_turn(self):
        frames = [
            observation(0, 0.0, 0.0, 5, "A"),
            observation(1, 0.2, 0.0, 5, "A"),
            observation(2, 0.4, 0.6, 1, "turn"),
            observation(3, 0.6, 0.6, 1, "turn"),
        ]
        frames += quiet_span(0.8, 3.0, 0.2, "B", frame_start=10)
        chosen = selector().select(frames)
        self.assertEqual([item.signature for item in chosen], ["B"])
        self.assertEqual(chosen[0].reason, "video_end")

    def test_empty_input_selects_nothing(self):
        self.assertEqual(selector().select([]), [])


if __name__ == "__main__":
    unittest.main()
