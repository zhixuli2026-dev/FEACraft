import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent } from "react";
import { api } from "../api";
import { captureProjectCase, useStore } from "../store";
import { newTikzDocument, nodeCommand, parseDiagram, replaceLines } from "../utils/tikzDiagram";
import type { DiagramNode } from "../utils/tikzDiagram";
import "./TikzEditor.css";

const directory = "E-report/latex/diagrams/";
const sceneWidth = 760;
const sceneHeight = 360;

export default function TikzEditor({ caseId, readOnly, onSaved }: {
  caseId: number; readOnly: boolean; onSaved: (path: string) => void;
}) {
  const tree = useStore(state => state.fileTree);
  const [text, setText] = useState(newTikzDocument);
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("input");
  const [filename, setFilename] = useState("diagram.tex");
  const [loaded, setLoaded] = useState<{ path: string; sha: string } | null>(null);
  const [from, setFrom] = useState("input");
  const [to, setTo] = useState("output");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const editing = useRef(false);
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const drag = useRef<{ id: string; scale: number; source: string } | null>(null);
  const parsed = useMemo(() => parseDiagram(text), [text]);
  const selected = parsed.nodes.find(node => node.id === current);
  const scale = Math.min(74, 300 / Math.max(1, ...parsed.nodes.map(node => Math.abs(node.x))),
    125 / Math.max(1, ...parsed.nodes.map(node => Math.abs(node.y))));
  const point = (node: Pick<DiagramNode, "x" | "y">, zoom = scale) =>
    [sceneWidth / 2 + node.x * zoom, sceneHeight / 2 - node.y * zoom] as const;

  useEffect(() => {
    const context = canvas.current?.getContext("2d");
    if (!open || !context) return;
    const css = getComputedStyle(canvas.current!);
    const color = (name: string) => css.getPropertyValue(name).trim();
    context.clearRect(0, 0, sceneWidth, sceneHeight);
    context.lineWidth = 2;
    context.strokeStyle = color("--vf-muted") || "#73869f";
    for (const link of parsed.links) {
      const fromNode = parsed.nodes.find(node => node.id === link.from);
      const toNode = parsed.nodes.find(node => node.id === link.to);
      if (!fromNode || !toNode) continue;
      const [x1, y1] = point(fromNode);
      const [x2, y2] = point(toNode);
      const angle = Math.atan2(y2-y1,x2-x1);
      const endX = x2-31*Math.cos(angle), endY = y2-31*Math.sin(angle);
      context.beginPath(); context.moveTo(x1,y1); context.lineTo(endX,endY);
      if (link.arrow) {
        context.lineTo(endX-10*Math.cos(angle-.44),endY-10*Math.sin(angle-.44));
        context.moveTo(endX,endY); context.lineTo(endX-10*Math.cos(angle+.44),endY-10*Math.sin(angle+.44));
      }
      context.stroke();
    }
    for (const node of parsed.nodes) {
      const [x,y] = point(node);
      context.strokeStyle = color(node.id===current ? "--vf-accent" : "--vf-muted") || "#6d8da5";
      context.fillStyle = color("--vf-panel") || "#17212a";
      context.beginPath();
      if (node.shape==="circle") context.arc(x,y,31,0,Math.PI*2);
      else context.roundRect(x-58,y-24,116,48,4);
      context.fill(); context.stroke();
      context.fillStyle = color("--vf-text") || "#eee";
      context.font = "16px system-ui";
      context.textAlign = "center"; context.textBaseline = "middle";
      context.fillText(node.label,x,y,node.shape==="circle" ? 53 : 105);
    }
  }, [open, parsed, current, scale]);

  function moveNode(event: PointerEvent<HTMLCanvasElement>, first: boolean) {
    if (readOnly || busy || (!first && !drag.current)) return;
    const box = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX-box.left)*sceneWidth/box.width;
    const y = (event.clientY-box.top)*sceneHeight/box.height;
    if (first) {
      const target = [...parsed.nodes].reverse().find(node => {
        const [left, top] = point(node);
        return Math.abs(left-x)<(node.shape==="circle" ? 32 : 59) && Math.abs(top-y)<32;
      });
      if (!target) return;
      setCurrent(target.id);
      drag.current = { id: target.id, scale, source: text };
      event.currentTarget.setPointerCapture(event.pointerId);
      return;
    }
    const initial = drag.current!;
    const document = parseDiagram(initial.source);
    const node = document.nodes.find(value => value.id === initial.id);
    if (node) setText(replaceLines(document,node.line,1,nodeCommand({ ...node,
      x: Math.round((x-sceneWidth/2)/initial.scale*100)/100, y: Math.round((sceneHeight/2-y)/initial.scale*100)/100 })));
  }

  function updateSelected(patch: Partial<DiagramNode>) {
    if (selected) setText(replaceLines(parsed,selected.line,1,nodeCommand({ ...selected, ...patch })));
  }

  function addNode() {
    const ids = new Set(parsed.nodes.map(node => node.id));
    let index = ids.size + 1;
    while (ids.has(`node${index}`)) index++;
    const id = `node${index}`;
    setText(replaceLines(parsed,parsed.end,0,nodeCommand({ id,x:0,y:1,label:`Node ${index}`,shape:"rectangle" })));
    setCurrent(id);
  }

  async function input(path: string) {
    const selection = captureProjectCase();
    if (!path || !selection?.isCurrent() || editing.current) return;
    editing.current = true; setBusy(true); setMessage(""); setError("");
    try {
      const file = await api.get<{ content: string; content_sha256: string }>(`/cases/${caseId}/file?path=${encodeURIComponent(path)}`);
      if (!selection.isCurrent()) return;
      setText(file.content); setCurrent(""); setFilename(path.slice(directory.length));
      setLoaded({ path, sha: file.content_sha256 });
    } catch (cause) { if (selection.isCurrent()) setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { editing.current = false; if (selection.isCurrent()) setBusy(false); }
  }

  async function save() {
    const selection = captureProjectCase();
    const name = filename.trim();
    if (!selection?.isCurrent() || editing.current || readOnly) return;
    if (!/^[\p{L}\p{N}_-][\p{L}\p{N} _.\-]{0,90}\.tex$/iu.test(name)) {
      setError("请输入以 .tex 结尾的文件名。" ); return;
    }
    const path = directory+name;
    editing.current = true; setBusy(true); setMessage(""); setError("");
    try {
      const value = await api.put<{ content_sha256: string }>(`/cases/${caseId}/file`,
        { path, content: text, expected_sha256: loaded?.path === path ? loaded.sha : null });
      if (!selection.isCurrent()) return;
      setLoaded({ path, sha: value.content_sha256 });
      onSaved(path);
      setMessage(`已保存 ${name}，并选作 LaTeX 主文件。`);
    } catch (cause) { if (selection.isCurrent()) setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { editing.current = false; if (selection.isCurrent()) setBusy(false); }
  }

  const editable = !readOnly && !busy && parsed.end > parsed.begin && parsed.begin >= 0;
  return <details className="vf-tikz" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>TikZ 图形编辑器</summary>
    {open && <div className="vf-tikz-body">
      <canvas ref={canvas} width={sceneWidth} height={sceneHeight} aria-label="TikZ 图形画布"
        onPointerDown={event => moveNode(event,true)} onPointerMove={event => moveNode(event,false)}
        onPointerUp={() => { drag.current=null; }} onLostPointerCapture={() => { drag.current=null; }} />
      <div className="vf-tikz-actions">
        <label>节点<select aria-label="节点" value={current} onChange={event => setCurrent(event.target.value)}>
          <option value="">选择节点</option>{parsed.nodes.map(node => <option key={node.id}>{node.id}</option>)}
        </select></label>
        <button type="button" disabled={!editable} onClick={addNode}>添加节点</button>
        {selected && <button type="button" disabled={!editable} onClick={() => {
          const removed = new Set([selected.line,...parsed.links.filter(link => link.from===selected.id || link.to===selected.id).map(link => link.line)]);
          setText(parsed.lines.filter((_,index) => !removed.has(index)).join(parsed.newline)); setCurrent("");
        }}>移除选中节点</button>}
      </div>
      {selected && <div className="vf-tikz-actions">
        <label>标签<input aria-label="标签" value={selected.label} disabled={!editable} onChange={event => updateSelected({ label: event.target.value })} /></label>
        <label>形状<select aria-label="形状" value={selected.shape} disabled={!editable} onChange={event =>
          updateSelected({ shape: event.target.value as DiagramNode["shape"] })}>
          <option value="rectangle">矩形</option><option value="circle">圆形</option>
        </select></label>
        <label>X<input type="number" aria-label="X" step="0.1" value={selected.x} disabled={!editable}
          onChange={event => { if (Number.isFinite(event.target.valueAsNumber)) updateSelected({ x: event.target.valueAsNumber }); }} /></label>
        <label>Y<input type="number" aria-label="Y" step="0.1" value={selected.y} disabled={!editable}
          onChange={event => { if (Number.isFinite(event.target.valueAsNumber)) updateSelected({ y: event.target.valueAsNumber }); }} /></label>
      </div>}
      <div className="vf-tikz-actions">
        <label>起点<select aria-label="起点" value={from} disabled={!editable} onChange={event => setFrom(event.target.value)}>
          <option value="">选择起点</option>{parsed.nodes.map(node => <option key={node.id}>{node.id}</option>)}
        </select></label>
        <label>终点<select aria-label="终点" value={to} disabled={!editable} onChange={event => setTo(event.target.value)}>
          <option value="">选择终点</option>{parsed.nodes.map(node => <option key={node.id}>{node.id}</option>)}
        </select></label>
        <button type="button" disabled={!editable || from===to || !parsed.nodes.some(node => node.id===from)
          || !parsed.nodes.some(node => node.id===to) || parsed.links.some(link => link.from===from && link.to===to)}
          onClick={() => setText(replaceLines(parsed,parsed.end,0,`  \\draw[->] (${from}) -- (${to});`))}>添加箭头</button>
      </div>
      <label>TikZ 源码<textarea aria-label="TikZ 源码" rows={9} value={text} disabled={busy || readOnly} onChange={event => setText(event.target.value)} /></label>
      {parsed.otherLines>0 && <p>{parsed.otherLines} 行由源码编辑，保存时保留。</p>}
      {parsed.begin<0 || parsed.end<0 ? <p>添加一个完整的 <code>tikzpicture</code> 环境即可可视编辑。</p> : null}
      <div className="vf-tikz-actions">
        <label>现有图形<select aria-label="现有图形" value="" disabled={busy} onChange={event => void input(event.target.value)}>
          <option value="">读取图形文件</option>
          {tree.filter(file => file.path.startsWith(directory) && /\.tex$/i.test(file.path) && !file.path.slice(directory.length).includes("/"))
            .map(file => <option key={file.path} value={file.path}>{file.path.slice(directory.length)}</option>)}
        </select></label>
        <label>文件名<input aria-label="文件名" value={filename} disabled={busy || readOnly} onChange={event => setFilename(event.target.value)} /></label>
        <button type="button" disabled={!editable} onClick={() => void save()}>保存并选择编译</button>
      </div>
      {message && <p role="status">{message}</p>}
      {error && <p role="alert">{error}</p>}
    </div>}
  </details>;
}
