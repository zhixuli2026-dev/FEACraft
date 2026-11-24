"""Animation 相机轨道：关键帧校验、插值、帧时间与非法输入。"""
import json

from app.services.animation import (AnimationError, camera_frame_times,
                                    canonical_camera_track_json,
                                    normalize_camera_track, sample_camera_track)

track = normalize_camera_track({
    "schema": "vibeflow.animation.camera.v1",
    "fps": 2,
    "loop": False,
    "frames": [
        {"t": 0, "position": [0, 0, 3], "focal": [0, 0, 0], "up": [0, 1, 0]},
        {"t": 1, "position": [2, 0, 3], "focal": [1, 0, 0], "up": [0, 1, 0]},
        {"t": 2, "position": [2, 2, 3], "focal": [1, 1, 0], "up": [0, 0, 1]},
    ],
})
mid = sample_camera_track(track, 0.5)
assert mid["position"] == [1, 0, 3] and mid["focal"] == [0.5, 0, 0]
assert mid["up"] == [0, 1, 0]
end = sample_camera_track(track, 99)
assert end["position"] == [2, 2, 3] and end["up"] == [0, 0, 1]
assert camera_frame_times(track, 2) == [0, 0.5, 1, 1.5, 2]
print("1. 关键帧插值、端点钳制与均匀帧时间 [OK]")

document = json.loads(canonical_camera_track_json(track))
assert document["schema"] == "vibeflow.animation.camera.v1" and document["fps"] == 2
assert normalize_camera_track(document) == track
print("2. 相机轨道 v1 导入导出 [OK]")

bad = [
    {"schema": "vibeflow.animation.camera.v1", "frames": []},
    {"schema": "vibeflow.animation.camera.v1", "frames": [{"t": 0, "position": [0, 0, 1], "focal": [0, 0, 0], "up": [0, 1, 0]}]},
    {"schema": "vibeflow.animation.camera.v1", "frames": [
        {"t": 0, "position": [0, 0, 1], "focal": [0, 0, 0], "up": [0, 1, 0]},
        {"t": 0, "position": [1, 0, 1], "focal": [0, 0, 0], "up": [0, 1, 0]}]},
    {"schema": "vibeflow.animation.camera.v1", "fps": 0, "frames": [
        {"t": 0, "position": [0, 0, 1], "focal": [0, 0, 0], "up": [0, 1, 0]},
        {"t": 1, "position": [1, 0, 1], "focal": [0, 0, 0], "up": [0, 1, 0]}]},
    {"schema": "vibeflow.animation.camera.v1", "frames": [
        {"t": 0, "position": [0, 0, 0], "focal": [0, 0, 0], "up": [0, 1, 0]},
        {"t": 1, "position": [1, 0, 1], "focal": [0, 0, 0], "up": [0, 1, 0]}]},
    {"schema": "vibeflow.animation.camera.v1", "frames": [
        {"t": 0, "position": [0, 0, 1], "focal": [0, 0, 0], "up": [0, 0, 0]},
        {"t": 1, "position": [1, 0, 1], "focal": [0, 0, 0], "up": [0, 1, 0]}]},
]
for value in bad:
    try:
        normalize_camera_track(value)
        raise AssertionError(f"非法轨道被接受: {value}")
    except AnimationError:
        pass
print("3. 帧数、重复时间、fps、位置焦点重合和零上向量均拒绝 [OK]")

print("\n=== Animation 相机轨道冒烟通过 ===")
