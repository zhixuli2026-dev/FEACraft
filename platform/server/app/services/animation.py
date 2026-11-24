"""Animation v1：相机关键帧校验、插值和帧时间。"""
from __future__ import annotations

import json
import math


class AnimationError(ValueError):
    pass


SCHEMA = "vibeflow.animation.camera.v1"


def _vec3(value, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise AnimationError(f"{label} 必须是三维向量")
    try:
        result = [float(v) for v in value]
    except (TypeError, ValueError) as error:
        raise AnimationError(f"{label} 必须是数值向量") from error
    if not all(math.isfinite(v) for v in result):
        raise AnimationError(f"{label} 必须有限")
    return result


def normalize_camera_track(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise AnimationError(f"相机轨道不是合法 JSON: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise AnimationError(f"相机轨道 schema 须为 {SCHEMA}")
    fps = value.get("fps", 2)
    try:
        fps = float(fps)
    except (TypeError, ValueError) as error:
        raise AnimationError("fps 必须是数值") from error
    if not math.isfinite(fps) or not 0.1 <= fps <= 60:
        raise AnimationError("fps 须在 0.1–60")
    frames = value.get("frames")
    if not isinstance(frames, list) or not 2 <= len(frames) <= 64:
        raise AnimationError("关键帧数量须为 2–64")
    normalized = []
    previous = -math.inf
    for item in frames:
        if not isinstance(item, dict):
            raise AnimationError("关键帧必须是对象")
        try:
            time_value = float(item["t"])
        except (KeyError, TypeError, ValueError) as error:
            raise AnimationError("关键帧缺少数值 t") from error
        if not math.isfinite(time_value) or time_value < 0 or time_value <= previous:
            raise AnimationError("关键帧 t 必须非负且严格递增")
        position = _vec3(item.get("position"), "position")
        focal = _vec3(item.get("focal"), "focal")
        up = _vec3(item.get("up"), "up")
        if position == focal:
            raise AnimationError("position 与 focal 不能重合")
        if math.sqrt(sum(v * v for v in up)) == 0:
            raise AnimationError("up 不能是零向量")
        normalized.append({"t": time_value, "position": position, "focal": focal, "up": up})
        previous = time_value
    return {"schema": SCHEMA, "fps": fps, "loop": bool(value.get("loop", False)), "frames": normalized}


def sample_camera_track(value, time_value: float) -> dict:
    track = normalize_camera_track(value)
    try:
        time_value = float(time_value)
    except (TypeError, ValueError) as error:
        raise AnimationError("采样时间必须是数值") from error
    if not math.isfinite(time_value):
        raise AnimationError("采样时间必须有限")
    frames = track["frames"]
    time_value = min(max(time_value, frames[0]["t"]), frames[-1]["t"])
    right = next((index for index, frame in enumerate(frames) if frame["t"] >= time_value), len(frames) - 1)
    left = max(0, right - 1)
    a, b = frames[left], frames[right]
    span = b["t"] - a["t"]
    alpha = 0 if span == 0 else (time_value - a["t"]) / span

    def interpolate(key):
        return [a[key][i] + (b[key][i] - a[key][i]) * alpha for i in range(3)]

    up = interpolate("up")
    length = math.sqrt(sum(v * v for v in up))
    if length == 0:
        raise AnimationError("插值得到零上向量，请调整关键帧")
    return {"position": interpolate("position"), "focal": interpolate("focal"),
            "up": [v / length for v in up]}


def camera_frame_times(value, fps: float | None = None) -> list[float]:
    track = normalize_camera_track(value)
    fps = track["fps"] if fps is None else float(fps)
    if not math.isfinite(fps) or not 0.1 <= fps <= 60:
        raise AnimationError("fps 须在 0.1–60")
    duration = track["frames"][-1]["t"] - track["frames"][0]["t"]
    steps = math.ceil(duration * fps - 1e-9)
    if steps > 1000:
        raise AnimationError("轨道导出超过 1000 帧")
    start = track["frames"][0]["t"]
    return [start + i / fps for i in range(steps)] + [track["frames"][-1]["t"]]


def canonical_camera_track_json(value) -> str:
    return json.dumps(normalize_camera_track(value), ensure_ascii=False, indent=2) + "\n"
