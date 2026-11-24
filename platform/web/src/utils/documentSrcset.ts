/** Conforming HTML candidates; commas inside image URLs are not separators. */
export function documentImageCandidates(value: string): { url: string; descriptor: string }[] {
  const space = (c: string) => ' \t\n\r\f'.includes(c);
  const candidates: { url: string; descriptor: string }[] = [];
  let position = 0, kind = '';
  const seen = new Set<number>();
  while (position < value.length) {
    while (position < value.length && space(value[position])) position++;
    if (position === value.length) break;
    const start = position;
    while (position < value.length && !space(value[position])) position++;
    let url = value.slice(start, position), descriptor = '';
    if (url.endsWith(',')) {
      url = url.slice(0, -1);
      if (!url || url.endsWith(',')) throw new Error('候选图片列表包含空项');
    } else {
      const begin = position;
      while (position < value.length && value[position] !== ',') position++;
      descriptor = value.slice(begin, position).replace(/^[ \t\n\r\f]+|[ \t\n\r\f]+$/g, '');
      if (position < value.length) position++;
    }
    if (!url || url.startsWith(',')) throw new Error('候选图片列表包含空项');
    let current = 'x', number = 1;
    if (descriptor) {
      if (/^[0-9]+w$/.test(descriptor)) current = 'w';
      else if (!/^(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?x$/.test(descriptor))
        throw new Error('候选图片描述符无效');
      number = Number(descriptor.slice(0, -1));
    }
    if (!Number.isFinite(number) || number <= 0 || (kind && kind !== current) || seen.has(number))
      throw new Error('候选图片描述符重复、混用或超出范围');
    seen.add(number); kind = current; candidates.push({ url, descriptor });
  }
  if (!candidates.length) throw new Error('候选图片列表为空');
  return candidates;
}
