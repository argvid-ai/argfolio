"""Turn a motion timeline into one selected frame per settled spread.

A settled period is saved when the next page turn is confirmed. The period
still on screen when the video ends is saved as well, so the last page does
not need another turn.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    frame_index: int
    timestamp: float
    motion: float
    sharpness: float
    signature: object


@dataclass(frozen=True)
class Selection:
    frame_index: int
    timestamp: float
    motion: float
    sharpness: float
    reason: str
    signature: object


@dataclass(frozen=True)
class RejectedDuplicate:
    frame_index: int
    timestamp: float
    sharpness: float


class PageSelector:
    def __init__(
        self,
        stable_seconds,
        motion_threshold,
        motion_confirm_frames,
        end_stable_seconds,
        is_duplicate,
    ):
        if motion_confirm_frames < 1:
            raise ValueError("motion_confirm_frames 必须 >= 1")
        if stable_seconds < 0 or end_stable_seconds < 0:
            raise ValueError("稳定时长不能为负")
        self.stable_seconds = float(stable_seconds)
        self.motion_threshold = float(motion_threshold)
        self.motion_confirm_frames = int(motion_confirm_frames)
        self.end_stable_seconds = float(end_stable_seconds)
        self.is_duplicate = is_duplicate
        self.rejections = []

    def select(self, observations):
        self.rejections = []
        selections = []
        quiet_frames = []
        state = "quiet"
        motion_run = 0

        for observation in observations:
            moving = observation.motion >= self.motion_threshold
            if state == "quiet":
                if moving:
                    motion_run += 1
                    if motion_run >= self.motion_confirm_frames:
                        self._emit(selections, quiet_frames, "turn_boundary", self.stable_seconds)
                        quiet_frames = []
                        state = "turning"
                        motion_run = 0
                else:
                    motion_run = 0
                    quiet_frames.append(observation)
                continue

            if moving:
                quiet_frames = []
                continue
            quiet_frames.append(observation)
            if _duration(quiet_frames) + 1e-9 >= self.stable_seconds:
                state = "quiet"
                motion_run = 0

        if quiet_frames:
            self._emit(selections, quiet_frames, "video_end", self.end_stable_seconds)
        return selections

    def _emit(self, selections, quiet_frames, reason, minimum_duration):
        if _duration(quiet_frames) + 1e-9 < minimum_duration:
            return
        best = max(quiet_frames, key=lambda item: (item.sharpness, -item.frame_index))
        if selections and self.is_duplicate(selections[-1].signature, best.signature):
            previous = selections[-1]
            if best.sharpness > previous.sharpness:
                selections[-1] = Selection(
                    frame_index=best.frame_index,
                    timestamp=best.timestamp,
                    motion=best.motion,
                    sharpness=best.sharpness,
                    reason=previous.reason,
                    signature=best.signature,
                )
            else:
                self.rejections.append(
                    RejectedDuplicate(
                        frame_index=best.frame_index,
                        timestamp=best.timestamp,
                        sharpness=best.sharpness,
                    )
                )
            return
        selections.append(
            Selection(
                frame_index=best.frame_index,
                timestamp=best.timestamp,
                motion=best.motion,
                sharpness=best.sharpness,
                reason=reason,
                signature=best.signature,
            )
        )


def _duration(frames):
    if len(frames) < 2:
        return 0.0
    return frames[-1].timestamp - frames[0].timestamp
