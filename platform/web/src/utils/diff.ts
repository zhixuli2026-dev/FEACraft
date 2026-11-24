export interface DiffRow {
  kind: "same" | "add" | "del" | "gap";
  text: string;
  noFinalNewline?: boolean;
}

export interface LineDiff {
  rows: DiffRow[];
  added: number;
  removed: number;
  truncated: boolean;
}

const MAX_COMPARISONS = 2_000_000;
const MAX_DIAGONALS = 120_000;

function lineRow(kind: "same" | "add" | "del", line: string): DiffRow {
  return { kind, text: line.replace(/\r?\n$/, ""), noFinalNewline: !line.endsWith("\n") };
}

function insertOnDiagonal(previous: Map<number, number>, diagonal: number, distance: number) {
  return diagonal === -distance || (diagonal !== distance &&
    (previous.get(diagonal - 1) ?? -1) < (previous.get(diagonal + 1) ?? -1));
}

function reconstruct(before: string[], after: string[], trace: Map<number, number>[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let x = before.length;
  let y = after.length;
  for (let distance = trace.length - 1; distance >= 0; distance--) {
    const diagonal = x - y;
    const previous = trace[distance];
    const inserted = insertOnDiagonal(previous, diagonal, distance);
    const previousDiagonal = diagonal + (inserted ? 1 : -1);
    const previousX = previous.get(previousDiagonal) ?? 0;
    const previousY = previousX - previousDiagonal;
    while (x > previousX && y > previousY) {
      rows.push(lineRow("same", before[--x]));
      y--;
    }
    if (distance > 0) {
      rows.push(inserted ? lineRow("add", after[--y]) : lineRow("del", before[--x]));
    }
  }
  return rows.reverse();
}

function shortestEdit(before: string[], after: string[]): DiffRow[] | null {
  const oldLines = new Set(before);
  if (!after.some(line => oldLines.has(line))) {
    return [...before.map(line => lineRow("del", line)), ...after.map(line => lineRow("add", line))];
  }

  // Myers shortest edit path. Bound both stored diagonals and repeated line comparisons.
  let previous = new Map([[1, 0]]);
  const trace: Map<number, number>[] = [];
  let comparisons = 0;
  let diagonals = 0;
  for (let distance = 0; distance <= before.length + after.length; distance++) {
    trace.push(previous);
    const frontier = new Map<number, number>();
    for (let diagonal = -distance; diagonal <= distance; diagonal += 2) {
      if (++diagonals > MAX_DIAGONALS) return null;
      const insert = insertOnDiagonal(previous, diagonal, distance);
      let x = insert ? previous.get(diagonal + 1)! : previous.get(diagonal - 1)! + 1;
      let y = x - diagonal;
      while (x < before.length && y < after.length) {
        if (++comparisons > MAX_COMPARISONS) return null;
        if (before[x] !== after[y]) break;
        x++;
        y++;
      }
      if (x >= before.length && y >= after.length) return reconstruct(before, after, trace);
      frontier.set(diagonal, x);
    }
    previous = frontier;
  }
  return null;
}

function collapseUnchanged(rows: DiffRow[]): DiffRow[] {
  const preview: DiffRow[] = [];
  for (let index = 0; index < rows.length;) {
    const row = rows[index];
    if (row.kind !== "same") {
      preview.push(row);
      index++;
      continue;
    }
    let end = index + 1;
    while (end < rows.length && rows[end].kind === "same") end++;
    const leading = index > 0 ? Math.min(2, end - index) : 0;
    const trailing = end < rows.length ? Math.min(2, end - index - leading) : 0;
    preview.push(...rows.slice(index, index + leading));
    const hidden = end - index - leading - trailing;
    if (hidden) preview.push({ kind: "gap", text: `省略 ${hidden} 行未改内容` });
    preview.push(...rows.slice(end - trailing, end));
    index = end;
  }
  return preview;
}

export function diffLines(oldText: string, newText: string, maxRows = 400): LineDiff | null {
  if (oldText === newText) return { rows: [], added: 0, removed: 0, truncated: false };
  // Include LF/CRLF in the comparison and avoid a phantom line after the final LF.
  const before = oldText.match(/[^\n]*\n|[^\n]+$/g) ?? [];
  const after = newText.match(/[^\n]*\n|[^\n]+$/g) ?? [];
  const rows = shortestEdit(before, after);
  if (!rows) return null;
  let added = 0;
  let removed = 0;
  for (const row of rows) {
    if (row.kind === "add") added++;
    if (row.kind === "del") removed++;
  }
  const preview = collapseUnchanged(rows);
  return { rows: preview.slice(0, maxRows), added, removed, truncated: preview.length > maxRows };
}
