import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import Papa from "papaparse";
import { api } from "../api";
import { useStore } from "../store";
import type { DelimitedResult } from "../utils/delimited";
import "./TablePanel.css";

const ROW_HEIGHT = 32;
const OVERSCAN = 6;
const numericPattern = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
const numberFormat = new Intl.NumberFormat("zh-CN");

type TableRead = { key: string; data: DelimitedResult } | { key: string; error: string };
type Sort = { column: number; ascending: boolean };

function numeric(value: string): number | undefined {
  const trimmed = value.trim();
  if (!numericPattern.test(trimmed)) return undefined;
  const number = Number(trimmed);
  return Number.isFinite(number) ? number : undefined;
}

function orderRows(rows: string[][], sort: Sort | null) {
  const indices = rows.map((_, index) => index);
  if (!sort) return indices;
  const keys = rows.map(row => {
    const text = row[sort.column] ?? "";
    return { text, number: numeric(text) };
  });
  return indices.sort((a, b) => {
    const x = keys[a], y = keys[b];
    if (x.text === "" || y.text === "") return Number(x.text === "") - Number(y.text === "");
    if ((x.number === undefined) !== (y.number === undefined)) return Number(x.number === undefined) - Number(y.number === undefined);
    const delta = x.number !== undefined && y.number !== undefined ? x.number - y.number : collator.compare(x.text, y.text);
    return sort.ascending ? delta : -delta;
  });
}

function downloadRows(filename: string, header: string[], body: string[][], indices: number[]) {
  const spreadsheetCell = (value: string) => numeric(value) === undefined && /^(?:[\t\r\n]|\s*[=+@-])/.test(value)
    ? "'" + value : value;
  const protect = (row: string[]) => header.map((_, column) => spreadsheetCell(row[column] ?? ""));
  const payload = Papa.unparse([protect(header), ...indices.map(index => protect(body[index]))]);
  const url = URL.createObjectURL(new Blob(["\ufeff", payload], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename.replace(/\.(csv|tsv)$/i, "") + "-导出.csv";
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function CsvGrid({ file, data }: { file: string; data: DelimitedResult }) {
  const { header, body, uneven } = useMemo(() => {
    const [first = [], ...body] = data.rows;
    let width = first.length;
    let uneven = 0;
    for (const row of body) {
      width = Math.max(width, row.length);
      if (row.length !== first.length) uneven++;
    }
    const header = Array.from({ length: width }, (_, index) => first[index] ?? `列 ${index + 1}`);
    return { header, body, uneven };
  }, [data]);
  const [query, setQuery] = useState("");
  const [filterColumn, setFilterColumn] = useState(-1);
  const [sort, setSort] = useState<Sort | null>(null);
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [detail, setDetail] = useState<number | null>(null);
  const [height, setHeight] = useState(320);
  const [scrollTop, setScrollTop] = useState(0);
  const surface = useRef<HTMLDivElement>(null);
  const selectAll = useRef<HTMLInputElement>(null);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const search = useDeferredValue(normalizedQuery);
  const filtering = search !== normalizedQuery;
  const sorted = useMemo(() => orderRows(body, sort), [body, sort]);
  const filtered = useMemo(() => !search ? sorted : sorted.filter(index => {
    const matches = (value: string) => value.toLocaleLowerCase().includes(search);
    return filterColumn < 0 ? body[index].some(matches) : matches(body[index][filterColumn] ?? "");
  }), [body, filterColumn, search, sorted]);
  const selectedInFilter = useMemo(() => filtered.reduce((count, index) => count + Number(selected.has(index)), 0), [filtered, selected]);
  const allSelected = filtered.length > 0 && selectedInFilter === filtered.length;

  useEffect(() => {
    const viewport = surface.current;
    if (!viewport) return;
    const resize = new ResizeObserver(() => setHeight(viewport.clientHeight));
    resize.observe(viewport);
    return () => resize.disconnect();
  }, []);
  useEffect(() => {
    if (surface.current) surface.current.scrollTop = 0;
    setScrollTop(0);
  }, [filtered]);
  useEffect(() => {
    if (selectAll.current) selectAll.current.indeterminate = selectedInFilter > 0 && !allSelected;
  }, [allSelected, selectedInFilter]);

  const windowSize = Math.ceil(height / ROW_HEIGHT) + OVERSCAN * 2 + 1;
  const start = Math.min(Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN), Math.max(0, filtered.length - windowSize));
  const end = Math.min(filtered.length, start + windowSize);
  const label = (column: number) => header[column] || `列 ${column + 1}`;
  const toggleRow = (index: number) => setSelected(current => {
    const next = new Set(current);
    if (next.has(index)) next.delete(index); else next.add(index);
    return next;
  });
  const toggleFiltered = () => setSelected(current => {
    const next = new Set(current);
    for (const index of filtered) {
      if (allSelected) next.delete(index); else next.add(index);
    }
    return next;
  });
  const exportFiltered = () => downloadRows(file, header, body, filtered);
  const exportSelected = () => downloadRows(file, header, body, sorted.filter(index => selected.has(index)));
  const jumpTo = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const row = Number(new FormData(event.currentTarget).get("row"));
    if (surface.current && Number.isInteger(row) && row >= 1 && row <= filtered.length) {
      surface.current.scrollTop = (row - 1) * ROW_HEIGHT;
    }
  };

  return <div className="csv-grid" data-testid="csv-grid">
    <div className="csv-toolbar">
      <span className="csv-summary" data-testid="csv-count" role="status">
        {numberFormat.format(filtered.length)} / {numberFormat.format(body.length)} 行 · {header.length} 列
      </span>
      <select value={filterColumn} aria-label="筛选列" onChange={event => setFilterColumn(Number(event.target.value))}>
        <option value={-1}>全部列</option>
        {header.map((_, column) => <option key={column} value={column}>{label(column)}</option>)}
      </select>
      <input type="search" aria-label="搜索表格" data-testid="csv-search" placeholder="搜索表格" value={query}
        onChange={event => setQuery(event.target.value)} />
    </div>
    {(data.errors.length > 0 || uneven > 0) && <details className="csv-warning" data-testid="csv-warning">
      <summary>解析警告</summary>
      {uneven > 0 && <p>{uneven} 行与表头列数不同，缺少的单元格留空。</p>}
      {data.errors.slice(0, 3).map((message, index) => <p key={index}>{message}</p>)}
      {data.errors.length > 3 && <p>另有 {data.errors.length - 3} 条解析警告。</p>}
    </details>}
    <div className="csv-viewport" ref={surface} tabIndex={0} aria-label="表格滚动区域" data-testid="csv-viewport" aria-busy={filtering}
      onScroll={event => setScrollTop(event.currentTarget.scrollTop)}>
      <table style={{ minWidth: 84 + header.length * 144 }} aria-label={file} aria-rowcount={filtered.length + 1}>
        <colgroup><col style={{ width: 84 }} />{header.map((_, column) => <col key={column} />)}</colgroup>
        <thead><tr aria-rowindex={1}>
          <th scope="col"><input type="checkbox" ref={selectAll} aria-label="选择全部筛选结果" checked={allSelected}
            disabled={filtering || !filtered.length} onChange={toggleFiltered} /></th>
          {header.map((_, column) => <th key={column} scope="col" data-testid="csv-header" data-column={column}
            aria-sort={sort?.column === column ? sort.ascending ? "ascending" : "descending" : undefined}>
            <button type="button" title={label(column)} onClick={() => setSort(value => value?.column !== column
              ? { column, ascending: true } : value.ascending ? { column, ascending: false } : null)}>
              <span>{label(column)}</span>{sort?.column === column && <span className="csv-sort-mark" aria-hidden="true">{sort.ascending ? "升序" : "降序"}</span>}
            </button>
          </th>)}
        </tr></thead>
        <tbody>
          {start > 0 && <tr aria-hidden="true"><td className="csv-spacer" colSpan={header.length + 1} style={{ height: start * ROW_HEIGHT }} /></tr>}
          {filtered.slice(start, end).map((index, offset) => <tr key={index} data-testid="csv-row" data-source-row={index + 1}
            data-selected={selected.has(index)} aria-rowindex={start + offset + 2}>
            <td><div className="csv-row-actions">
              <input type="checkbox" aria-label={`选择第 ${index + 1} 行`} checked={selected.has(index)} onChange={() => toggleRow(index)} />
              <button type="button" title={`查看源数据第 ${index + 1} 行`} aria-label={`查看第 ${index + 1} 行`}
                onClick={() => setDetail(index)}>{index + 1}</button>
            </div></td>
            {header.map((_, column) => {
              const value = body[index][column] ?? "";
              return <td key={column} title={value} className={numeric(value) !== undefined ? "csv-number" : undefined}>
                {value.replace(/[\r\n]+/g, " ")}
              </td>;
            })}
          </tr>)}
          {end < filtered.length && <tr aria-hidden="true"><td className="csv-spacer" colSpan={header.length + 1} style={{ height: (filtered.length - end) * ROW_HEIGHT }} /></tr>}
        </tbody>
      </table>
      {!filtered.length && <p className="csv-message" role="status">{body.length ? "没有匹配的行。" : "表格只有列标题。"}</p>}
    </div>
    <div className="csv-toolbar csv-footer">
      <form className="csv-jump" onSubmit={jumpTo}>
        <input type="number" name="row" aria-label="筛选结果中的行号" min={1} max={filtered.length} required disabled={filtering || !filtered.length} />
        <button type="submit" disabled={filtering || !filtered.length}>跳转到行</button>
      </form>
      <span className="csv-selection" data-testid="csv-selection">已选 {numberFormat.format(selected.size)} 行</span>
      <button type="button" onClick={exportFiltered} disabled={filtering || !filtered.length}>导出筛选结果</button>
      <button type="button" onClick={exportSelected} disabled={!selected.size}>导出所选</button>
    </div>
    {detail !== null && <section className="csv-detail" aria-label={`第 ${detail + 1} 行完整内容`} data-testid="csv-detail">
      <div className="csv-detail-header"><strong>源数据第 {detail + 1} 行</strong><button type="button" onClick={() => setDetail(null)}>关闭行详情</button></div>
      <dl>{header.map((_, column) => <div key={column}><dt>{label(column)}</dt><dd>{body[detail][column] ?? ""}</dd></div>)}</dl>
    </section>}
  </div>;
}

export function TablePanel() {
  const file = useStore(state => state.activeFile);
  const caseId = useStore(state => state.currentCase?.id);
  const userId = useStore(state => state.user?.id);
  const [reload, setReload] = useState(0);
  const [read, setRead] = useState<TableRead | null>(null);
  const key = JSON.stringify([userId, caseId, file, reload]);

  useEffect(() => {
    if (!file || !caseId || !userId) return;
    let cancelled = false;
    let worker: Worker | undefined;
    const fail = (cause: unknown) => {
      if (!cancelled) setRead({ key, error: cause instanceof Error ? cause.message : String(cause) });
    };
    void api.get<{ content?: string }>(`/cases/${caseId}/file?path=${encodeURIComponent(file)}`).then(response => {
      if (cancelled) return;
      if (typeof response.content !== "string") throw new Error("此文件无法作为 CSV 或 TSV 文本读取。");
      worker = new Worker(new URL("../utils/delimitedWorker.ts", import.meta.url), { type: "module" });
      worker.onmessage = ({ data }: MessageEvent<DelimitedResult>) => {
        worker?.terminate();
        if (!cancelled) setRead({ key, data });
      };
      worker.onerror = () => {
        worker?.terminate();
        fail("表格解析未完成。");
      };
      worker.postMessage({ content: response.content, delimiter: file.toLowerCase().endsWith(".tsv") ? "\t" : "" });
    }).catch(fail);
    return () => { cancelled = true; worker?.terminate(); };
  }, [caseId, file, key, userId]);

  if (!file || !caseId) return <p className="csv-message">未选择文件</p>;
  const current = read?.key === key ? read : null;
  return <div className="csv-viewer" data-testid="csv-viewer" data-file={file} aria-busy={!current}>
    {!current ? <p className="csv-message" role="status">正在读取表格…</p> : "error" in current ?
      <div className="csv-message"><p role="alert" data-testid="csv-error">表格读取失败。{current.error}</p>
        <button type="button" onClick={() => setReload(value => value + 1)}>重新读取</button></div> :
      current.data.rows.length ? <CsvGrid key={key} file={file.split("/").pop() ?? file} data={current.data} /> :
        <p className="csv-message" role="status">文件没有表格数据。</p>}
  </div>;
}
