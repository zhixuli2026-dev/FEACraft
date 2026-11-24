const maxEdge = 2048;
const maxPixels = 1_000_000;
const maxImageBytes = 4 * 1024 * 1024;

function visibleBounds(screen: HTMLElement, view: Window): DOMRect {
  const rect = screen.getBoundingClientRect();
  let left = Math.max(0, rect.left);
  let top = Math.max(0, rect.top);
  let right = Math.min(view.innerWidth, rect.right);
  let bottom = Math.min(view.innerHeight, rect.bottom);
  for (let parent = screen.parentElement; parent; parent = parent.parentElement) {
    const { overflowX, overflowY } = view.getComputedStyle(parent);
    if (overflowX === 'visible' && overflowY === 'visible') continue;
    const box = parent.getBoundingClientRect();
    const clipLeft = box.left + parent.clientLeft;
    const clipTop = box.top + parent.clientTop;
    if (overflowX !== 'visible') {
      left = Math.max(left, clipLeft);
      right = Math.min(right, clipLeft + parent.clientWidth);
    }
    if (overflowY !== 'visible') {
      top = Math.max(top, clipTop);
      bottom = Math.min(bottom, clipTop + parent.clientHeight);
    }
  }
  return new DOMRect(left, top, Math.max(0, right - left), Math.max(0, bottom - top));
}

function paintVisibleRows(ctx: CanvasRenderingContext2D, rows: Element, bounds: DOMRect, view: Window): boolean {
  let hasOutput = false;
  for (const row of Array.from(rows.children)) {
    const box = row.getBoundingClientRect();
    if (box.bottom <= bounds.top || box.top >= bounds.bottom) continue;
    ctx.save();
    ctx.beginPath();
    ctx.rect(box.left, box.top, box.width, box.height);
    ctx.clip();
    for (const span of Array.from(row.children)) {
      const cell = span.getBoundingClientRect();
      if (cell.right <= bounds.left || cell.left >= bounds.right) continue;
      const style = view.getComputedStyle(span);
      if (style.visibility !== 'visible') continue;
      if (style.backgroundColor !== 'rgba(0, 0, 0, 0)') {
        ctx.fillStyle = style.backgroundColor;
        ctx.fillRect(cell.left, cell.top, cell.width, cell.height);
        hasOutput = true;
      }
      const value = span.textContent ?? '';
      const decorations = style.textDecorationLine.split(' ');
      if (!value.trim() && style.textDecorationLine === 'none') continue;

      ctx.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
      ctx.letterSpacing = style.letterSpacing === 'normal' ? '0px' : style.letterSpacing;
      const metrics = ctx.measureText(value);
      const ascent = metrics.fontBoundingBoxAscent;
      const descent = metrics.fontBoundingBoxDescent;
      const baseline = box.top + (box.height + ascent - descent) / 2;
      ctx.fillStyle = style.color;
      ctx.fillText(value, cell.left, baseline);
      ctx.fillStyle = style.textDecorationColor;
      const stroke = Math.max(1, parseFloat(style.textDecorationThickness) || 1);
      if (decorations.includes('underline')) ctx.fillRect(cell.left, baseline + 1, cell.width, stroke);
      if (decorations.includes('overline')) ctx.fillRect(cell.left, baseline - ascent, cell.width, stroke);
      if (decorations.includes('line-through')) ctx.fillRect(cell.left, baseline - ascent * .35, cell.width, stroke);
      hasOutput = true;
    }
    ctx.restore();
  }
  return hasOutput;
}

export async function captureTerminalScreenshot(host: HTMLElement): Promise<File> {
  const doc = host.ownerDocument;
  const view = doc.defaultView;
  const screen = host.querySelector<HTMLElement>('.xterm-screen');
  const rows = screen?.querySelector(':scope > .xterm-rows');
  if (!view || !host.isConnected || !screen || !rows || view.getComputedStyle(screen).visibility !== 'visible') {
    throw new Error('当前没有可截图的终端。');
  }
  const bounds = visibleBounds(screen, view);
  if (bounds.width < 1 || bounds.height < 1) throw new Error('当前没有可截图的终端。');

  // xterm's DOM renderer contains only the current viewport. Avoid its scrollback,
  // accessibility mirror and IME helpers, even when they still contain text.
  const canvas = doc.createElement('canvas');
  const scale = Math.min(2, view.devicePixelRatio, maxEdge / Math.max(bounds.width, bounds.height),
    Math.sqrt(maxPixels / (bounds.width * bounds.height)));
  canvas.width = Math.max(1, Math.floor(bounds.width * scale));
  canvas.height = Math.max(1, Math.floor(bounds.height * scale));
  const ctx = canvas.getContext('2d', { alpha: false });
  if (!ctx) throw new Error('浏览器无法生成终端截图。');
  ctx.fillStyle = view.getComputedStyle(host).backgroundColor;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.setTransform(scale, 0, 0, scale, -bounds.left * scale, -bounds.top * scale);
  ctx.fontKerning = 'none';
  if (!paintVisibleRows(ctx, rows, bounds, view)) throw new Error('当前终端没有可截图的输出。');

  const png = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/png'));
  if (!png?.size) throw new Error('浏览器无法生成终端截图。');
  if (png.size > maxImageBytes) throw new Error('终端截图超过 4 MiB。');
  const stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-');
  return new File([png], `terminal-${stamp}.png`, { type: 'image/png' });
}
