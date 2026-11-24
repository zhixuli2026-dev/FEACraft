import { useEffect, useState } from "react";

export type AnimationResult = { fps: number; frames: { t: number; path: string; points?: number }[];
  flipbook?: string[]; streaming?: number };

export default function ParaViewAnimationFrames({ caseId, animation }: { caseId: number; animation: AnimationResult }) {
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [image, setImage] = useState<{ index: number; url: string } | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const frame = animation.frames[index];
  const title = animation.streaming ? "粒子分块预览" : animation.flipbook ? "对照播放" : "动画帧";
  const current = image?.index === index ? image.url : null;

  useEffect(() => {
    const controller = new AbortController();
    const token = localStorage.getItem("vf_token");
    let url = "";
    void (async () => {
      try {
        const response = await fetch("/api/v1/cases/" + caseId + "/raw?path=" + encodeURIComponent(frame.path),
          { signal: controller.signal, headers: token ? { Authorization: "Bearer " + token } : {} });
        if (!response.ok) throw new Error("HTTP " + response.status);
        const blob = await response.blob();
        if (!controller.signal.aborted) {
          url = URL.createObjectURL(blob);
          setImage({ index, url });
        }
      } catch (cause) {
        if (!controller.signal.aborted) {
          setPlaying(false);
          setError("读取动画帧失败。" + String(cause instanceof Error ? cause.message : cause));
        }
      }
    })();
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [caseId, frame.path, index, retry]);

  useEffect(() => {
    if (!playing || !current) return;
    const next = animation.frames[index + 1];
    if (!next && !animation.flipbook) { setPlaying(false); return; }
    const timer = setTimeout(() => setIndex(next ? index + 1 : 0), (next ? next.t-frame.t : 1/animation.fps)*1000);
    return () => clearTimeout(timer);
  }, [animation.frames, animation.flipbook, animation.fps, current, frame.t, index, playing]);

  return <section className="vf-pv-animation" aria-label={`ParaView ${title}`}
    tabIndex={animation.flipbook ? 0 : undefined} onKeyDown={event => {
      if (animation.flipbook && event.target === event.currentTarget && event.key === " ") {
        event.preventDefault(); setPlaying(false); setError(""); setIndex((index+1)%animation.frames.length);
      }
    }}>
    <h4>{title}</h4>
    <div className="vf-pv-actions">
      <button type="button" disabled={!current} onClick={() => {
        if (!playing && !animation.flipbook && index === animation.frames.length - 1) setIndex(0);
        setPlaying(!playing);
      }}>{playing ? "暂停动画" : "播放动画"}</button>
      <output aria-live={playing ? "off" : "polite"}>{animation.streaming ? `${frame.points} 个点 · `
        : !animation.flipbook && `t = ${Number(frame.t.toPrecision(8))} s · `}{index + 1}/{animation.frames.length}</output>
    </div>
    <input type="range" aria-label="动画帧进度" min={0} max={animation.frames.length - 1} step={1}
      value={index} onChange={event => { setPlaying(false); setError(""); setIndex(Number(event.target.value)); }} />
    {current && <img src={current} alt={"ParaView 动画帧 " + (index + 1)} width="640" height="480" />}
    {error ? <div role="alert"><p>{error}</p><button type="button"
      onClick={() => { setError(""); setRetry(value => value + 1); }}>重读帧</button></div>
      : !current && <p role="status">加载动画帧…</p>}
  </section>;
}
