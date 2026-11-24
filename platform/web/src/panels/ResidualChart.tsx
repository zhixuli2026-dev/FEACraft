import { useMemo, useRef, useState } from 'react';

export interface ParsedLog {
  times: number[];
  courant: [number, number][];
  residuals: Record<string, number[]>;
  residuals_detail?: Record<string, {initial: number; final: number; iterations: number}[]>;
  summary: {
    time_points: number; last_time: number | null; fields: string[];
    iterations_total: number; courant_max_last: number | null;
    execution_time_last_s: number | null; finished_cleanly: boolean;
    fatal_errors?: number; warnings?: number;
  };
}

const WIDTH = 640, LEFT = 60, RIGHT = 14, BOTTOM = 32;
const COLORS = ['#60a5fa', '#4ade80', '#fbbf24', '#f87171', '#c4b5fd', '#67e8f9', '#f9a8d4'];
const PLOT_WIDTH = WIDTH - LEFT - RIGHT;

type Point = {index: number; value: number};
type Trace = {length: number; valueAt: (index: number) => number | undefined};

function traces(data: ParsedLog) {
  const details = data.residuals_detail ?? {};
  return [...new Set([...Object.keys(details), ...Object.keys(data.residuals)])].map(name => {
    const rows = details[name];
    const finals = data.residuals[name];
    const initial: Trace = {length: rows?.length ?? 0, valueAt: index => rows?.[index]?.initial};
    const final: Trace = {length: Math.max(rows?.length ?? 0, finals?.length ?? 0),
      valueAt: index => rows?.[index]?.final ?? finals?.[index]};
    return {name, initial, final};
  });
}

function usable(value: number | undefined, logarithmic: boolean): value is number {
  return value !== undefined && Number.isFinite(value) && (!logarithmic || value > 0);
}

function seriesSegments(trace: Trace, logarithmic: boolean, iterations: number) {
  const segments: Point[][] = [];
  let current: Point[] = [];
  const add = (points: Point[]) => {
    if (points.length) current.push(...points);
    else if (current.length) {
      segments.push(current);
      current = [];
    }
  };
  // Limit drawn points to first, extreme and last samples within each pixel column.
  // This preserves spikes while avoiding a large SVG string for a long run.
  const stride = trace.length > 2 * PLOT_WIDTH ? Math.ceil((iterations - 1) / PLOT_WIDTH) : 1;
  for (let start = 0; start < trace.length; start += stride) {
    let first: Point | undefined, last: Point | undefined, low: Point | undefined, high: Point | undefined;
    for (let index = start; index < Math.min(start + stride, trace.length); index++) {
      const value = trace.valueAt(index);
      if (!usable(value, logarithmic)) continue;
      const point = {index, value};
      first ??= point;
      last = point;
      if (!low || value < low.value) low = point;
      if (!high || value > high.value) high = point;
    }
    add(first ? [...new Set([first, low!, high!, last!])].sort((a, b) => a.index - b.index) : []);
  }
  if (current.length) segments.push(current);
  return segments;
}

function plot(data: ParsedLog, logarithmic: boolean) {
  const fields = traces(data);
  let minimum = Infinity, maximum = -Infinity, iterations = 1, hasNonPositive = false;
  for (const {initial, final} of fields) {
    iterations = Math.max(iterations, initial.length, final.length);
    for (const trace of [initial, final]) {
      for (let index = 0; index < trace.length; index++) {
        const value = trace.valueAt(index);
        if (value !== undefined && Number.isFinite(value) && value <= 0) hasNonPositive = true;
        if (!usable(value, logarithmic)) continue;
        minimum = Math.min(minimum, value);
        maximum = Math.max(maximum, value);
      }
    }
  }
  if (!Number.isFinite(minimum)) return null;

  const unit = logarithmic ? 1 : Math.max(Math.abs(minimum), Math.abs(maximum)) || 1;
  const transform = (value: number) => logarithmic ? Math.log10(value) : value / unit;
  const low = logarithmic ? Math.floor(transform(minimum)) : Math.min(0, transform(minimum));
  const high = logarithmic ? Math.ceil(transform(maximum)) : Math.max(0, transform(maximum));
  const lower = low === high && logarithmic ? low - 1 : low;
  const upper = low === high && !logarithmic ? 1 : high;
  const ticks = [];
  if (logarithmic) {
    const step = Math.max(1, Math.ceil((upper - lower) / 8));
    for (let tick = lower; tick < upper; tick += step) ticks.push(tick);
    ticks.push(upper);
  } else {
    for (let index = 0; index <= 5; index++) ticks.push(lower + (upper - lower) * index / 5);
  }

  const top = 12 + Math.ceil(fields.length / 6) * 16;
  const height = 260 + Math.max(0, top - 28);
  const bottom = height - BOTTOM;
  const x = (index: number) => LEFT + index / Math.max(1, iterations - 1) * PLOT_WIDTH;
  const axisY = (coordinate: number) => top + (upper - coordinate) / (upper - lower) * (bottom - top);
  return {
    height, top, bottom, iterations, hasNonPositive,
    ticks: ticks.map(tick => ({y: axisY(tick), label: logarithmic ? `1e${tick}` : (tick * unit).toExponential(1)})),
    fields: fields.map(({name, initial, final}) => ({
      name,
      traces: [initial, final].map(trace => seriesSegments(trace, logarithmic, iterations)
        .map(segment => segment.map(point => ({x: x(point.index), y: axisY(transform(point.value))})))),
    })),
  };
}

export default function ResidualChart({data}: {data: ParsedLog}) {
  const [logarithmic, setLogarithmic] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState('');
  const svgRef = useRef<SVGSVGElement>(null);
  const chart = useMemo(() => plot(data, logarithmic), [data, logarithmic]);
  const hasFields = Object.keys(data.residuals).length > 0 || Object.keys(data.residuals_detail ?? {}).length > 0;
  const {summary} = data;

  async function exportPng() {
    const svg = svgRef.current;
    if (!svg || !chart) return;
    setExporting(true);
    setExportError('');
    const xml = new XMLSerializer().serializeToString(svg);
    const source = URL.createObjectURL(new Blob([xml], {type: 'image/svg+xml;charset=utf-8'}));
    try {
      const img = new Image();
      img.src = source;
      await img.decode();
      const canvas = document.createElement('canvas');
      canvas.width = WIDTH * 2;
      canvas.height = chart.height * 2;
      const context = canvas.getContext('2d');
      if (!context) throw new Error('Canvas is unavailable');
      context.drawImage(img, 0, 0, canvas.width, canvas.height);
      const link = document.createElement('a');
      link.href = canvas.toDataURL('image/png');
      link.download = `residual_t${summary.last_time ?? 'unknown'}.png`;
      link.click();
    } catch {
      setExportError('PNG 导出失败，请重试。');
    } finally {
      URL.revokeObjectURL(source);
      setExporting(false);
    }
  }

  return <section className="vf-residual" aria-label="残差与日志摘要">
    <div className="vf-residual-summary" data-testid="residual-summary">
      <span>时刻点 {summary.time_points}</span>
      <span>末时刻 t={summary.last_time ?? '—'}</span>
      <span>残差记录 {summary.iterations_total}</span>
      {summary.courant_max_last != null && <span>Courant max {summary.courant_max_last.toExponential(2)}</span>}
      {summary.execution_time_last_s != null && <span>耗时 {summary.execution_time_last_s.toFixed(1)}s</span>}
      {!!summary.fatal_errors && <span>致命错误 {summary.fatal_errors}</span>}
      {!!summary.warnings && <span>警告 {summary.warnings}</span>}
      <span>{summary.finished_cleanly ? '正常结束' : '未见 End 标记'}</span>
    </div>
    {hasFields && <div className="vf-residual-controls">
      <button type="button" aria-pressed={logarithmic} onClick={() => setLogarithmic(true)}>对数轴</button>
      <button type="button" aria-pressed={!logarithmic} onClick={() => setLogarithmic(false)}>线性轴</button>
      <button type="button" onClick={() => void exportPng()} disabled={!chart || exporting}>
        {exporting ? '正在导出…' : '导出 PNG'}
      </button>
    </div>}
    {exportError && <p role="alert">{exportError}</p>}
    {logarithmic && chart?.hasNonPositive && <p className="vf-residual-note">对数轴显示正值。其余数值可在线性轴查看。</p>}
    {!chart ? <p role="status" className="vf-residual-note">
      {hasFields && logarithmic ? '没有可显示的正残差，请切换到线性轴。' : '日志里没有可解析的残差序列。'}
    </p> : <svg ref={svgRef} role="img" aria-label="求解残差曲线" xmlns="http://www.w3.org/2000/svg"
      width={WIDTH} height={chart.height} viewBox={`0 0 ${WIDTH} ${chart.height}`} className="vf-residual-plot">
      <rect width={WIDTH} height={chart.height} rx="6" fill="#0b0d12" />
      {chart.fields.map(({name}, index) => {
        const x = LEFT + index % 6 * 92, y = 17 + Math.floor(index / 6) * 16;
        const color = COLORS[index % COLORS.length];
        return <g key={name}>
          <title>{name}</title>
          <line x1={x} x2={x + 10} y1={y - 3} y2={y - 3} stroke={color} strokeWidth={2} />
          <text x={x + 14} y={y} fontSize="10" fill={color}>{name.length > 12 ? name.slice(0, 11) + '…' : name}</text>
        </g>;
      })}
      {chart.ticks.map(({label, y}, index) => <g key={index}>
        <line x1={LEFT} x2={WIDTH - RIGHT} y1={y} y2={y} stroke="#303847" />
        <text x={LEFT - 6} y={y + 3} textAnchor="end" fontSize="10" fill="#aab6c8">{label}</text>
      </g>)}
      <line x1={LEFT} x2={LEFT} y1={chart.top} y2={chart.bottom} stroke="#58667a" />
      {chart.fields.map(({name, traces}, index) => traces.map((segments, trace) => {
        const color = COLORS[index % COLORS.length];
        const attrs = {'data-field': name, 'data-series': trace === 0 ? 'initial' : 'final'};
        return <g key={`${name}/${trace}`} {...attrs}>
          {segments.map((segment, i) => segment.length === 1
            ? <circle key={i} cx={segment[0].x} cy={segment[0].y} r={trace === 0 ? 1.5 : 2.5} fill={color} />
            : <polyline key={i} points={segment.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')}
                fill="none" stroke={color} strokeWidth={trace === 0 ? 1 : 1.8}
                strokeDasharray={trace === 0 ? '3 2' : undefined} />)}
        </g>;
      }))}
      <text x={LEFT} y={chart.bottom + 13} fontSize="10" fill="#aab6c8">1</text>
      {chart.iterations > 1 && <text x={WIDTH - RIGHT} y={chart.bottom + 13} textAnchor="end"
        fontSize="10" fill="#aab6c8">{chart.iterations}</text>}
      <text x={LEFT} y={chart.height - 5} fontSize="10" fill="#aab6c8">虚线 initial · 实线 final</text>
      <text x={WIDTH - RIGHT} y={chart.height - 5} textAnchor="end" fontSize="10" fill="#aab6c8">迭代步</text>
    </svg>}
  </section>;
}
