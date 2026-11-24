# 时间、相机与 ParaView 属性动画（WP07 / D07）

2026-11-23 按 T06 验收数据时刻、相机与原生属性轨道。

## 1. 格式

```json
{
  "schema": "vibeflow.animation.camera.v1",
  "fps": 2,
  "loop": false,
  "frames": [
    {"t": 0, "position": [0, 0, 3], "focal": [0, 0, 0], "up": [0, 1, 0]},
    {"t": 1, "position": [3, 0, 3], "focal": [0.5, 0, 0], "up": [0, 1, 0]},
    {"t": 2, "position": [3, 3, 3], "focal": [0.5, 0.5, 0], "up": [0, 0, 1]}
  ]
}
```

- 关键帧数为 2–64，`t` 非负且严格递增；fps 为 0.1–60。
- position/focal/up 都是有限三维向量；position 与 focal 不能重合，up 不能为零向量。
- 播放按时间线性插值 position/focal/up，up 归一化；首末时间之外按端点钳制。
- 导出帧时间从首个 t 到最后 t，按 fps 均匀采样并包含末帧，最多 1000 帧。

## 2. viz 操作

在右侧“相机轨道”编辑 JSON：

- **应用轨道**：校验并移动到首帧。
- **播放轨道/暂停**：按独立相机时间轴异步播放，不改数据时间轴；可启用循环。
- **导出轨道**：逐帧写入 `D-post/animations/camera_000.png` 起的 PNG，结束后恢复导出前视角。

数据时刻播放/导帧与相机轨道是两条独立功能，不能互相替代。

## 3. 验证

- `animation_smoke.py`：插值、端点、均匀帧时间、v1 往返及非法轨道。
- `e2e-animation.mjs`：应用、播放/暂停、5 帧导出，首末 PNG hash 不同；`shots/wp07-animation-focused.png` 与 `shots/wp07-animation-focused-frame-004.png` 已人工核对。

## 4. 视角控制

独立“视角”区提供 +X/+Y/+Z/等轴测、保存/恢复当前 position/focal/up 和 `view-preset.png` 截图。`e2e-camera.mjs` 已验证预设图像不同，恢复后的 SHA-256 与保存时一致；`shots/wp07-camera-focused.png` 已人工核对。

## 5. ParaView 属性关键帧

在 ParaView 属性检查器选择算子，展开「属性关键帧」并应用。浏览器可播放、暂停、拖动帧，或在算例的 `D-post/paraview-pipelines/<版本>/frames/` 下载 PNG。`pipeline.json` 保存实际采样时刻、点数、范围和归一化轨道，`pipeline.pvsm` 保存起始管线。API `POST /api/v1/cases/{case_id}/paraview/pipeline` 在 `animation` 字段接收相同内容。

```json
{
  "fps": 4,
  "tracks": [
    {"node": "sphere", "property": "Radius", "frames": [{"t": 0, "value": 0.5}, {"t": 1, "value": 1.5}]},
    {"node": "@display", "property": "Opacity", "frames": [{"t": 0, "value": 0.8}, {"t": 1, "value": 0.4}]}
  ]
}
```

`node` 对应当前管线内的节点 ID。数值及等长数值向量可用 `linear` 或 `smooth`，离散属性用 `step`。`camera` 可附加第 1 节的相机轨道，按同一时刻采样。默认视角根据整段实际几何范围取景，导出帧都保留可见主体。

Windows 受管 `pvpython` 组合半径、向量中心、离散分辨率、透明度与相机，5 帧点数从 74 变为 122。非特权 Linux 容器实跑 5 帧，中心图像的非背景像素从 3,797 增至 34,059。Edge 验证真实帧的播放和拖动。旧数据时刻导出两帧，相机导出五帧，导出后恢复原视角。 [原生与浏览器验收](../../tmp/FF-D03-D05-server-manager-20261123/d07-acceptance.json) · [旧轨道复验](../../tmp/FF-D03-D05-server-manager-20261123/legacy-tracks.json) · [Edge 播放截图](../../tmp/ff-d03-pipeline-edge-md1bvp5r/paraview-animation.png)
