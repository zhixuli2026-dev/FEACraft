import JSZip from 'jszip';
import mammoth from 'mammoth';
import { docxSvgCompatibility } from './docxSvgCompatibility';

const OMML = 'http://schemas.openxmlformats.org/officeDocument/2006/math';
const WORD = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
const MATHML = 'http://www.w3.org/1998/Math/MathML';
const children = (e: Element) => [...e.children].filter(c => c.namespaceURI === OMML);
const child = (e: Element, name: string) => children(e).find(c => c.localName === name);
const value = (e: Element | undefined, name: string, fallback = '') =>
  (e && child(e, name)?.getAttributeNS(OMML, 'val')) ?? fallback;
const enabled = (v: string) => !['0', 'false', 'off'].includes(v);

/** Structural OMML conversion. Never flatten an unknown operator into a
 * plausible but mathematically different equation. No input markup is copied.
 */
function equation(source: Element): Element {
  const dom = document.implementation.createDocument(MATHML, 'math');
  const node = (tag: string, contents: (Node | string)[] = [], attrs: Record<string, string> = {}) => {
    const result = dom.createElementNS(MATHML, tag);
    for (const [key, val] of Object.entries(attrs)) result.setAttribute(key, val);
    for (const part of contents) result.append(typeof part === 'string' ? dom.createTextNode(part) : part);
    return result;
  };
  const row = (parts: Node[]) => node('mrow', parts);
  function convert(e: Element): Element {
    const name = e.localName;
    const props = child(e, name + 'Pr');
    const prop = (key: string, fallback = '') => value(props, key, fallback);
    const arg = (key: string) => {
      const part = child(e, key);
      if (!part) throw new Error('缺少公式参数 ' + key);
      return convert(part);
    };
    const content = () => children(e).filter(c => !c.localName.endsWith('Pr')).map(convert);
    switch (name) {
      case 'oMathPara': case 'oMath': case 'e': case 'num': case 'den': case 'sub':
      case 'sup': case 'deg': case 'lim': case 'fName': case 'box':
        return row(content());
      case 'r': {
        const text = children(e).filter(c => c.localName === 't').map(c => c.textContent ?? '').join('');
        if (child(props ?? e, 'nor') && enabled(prop('nor', '1'))) return node('mtext', [text]);
        const variant: Record<string, string> = {p:'normal', b:'bold', bi:'bold-italic', i:'italic'};
        const scripts: Record<string, string> = {roman:'normal', script:'script', fraktur:'fraktur',
          'double-struck':'double-struck', 'sans-serif':'sans-serif', monospace:'monospace'};
        const style = prop('sty'); const script = prop('scr');
        const attrs: Record<string, string> = style || script ? {mathvariant: scripts[script] ?? variant[style] ?? 'normal'} : {};
        const tokens = text.match(/\d+(?:\.\d+)?|\s+|[\s\S]/gu) ?? [];
        return row(tokens.map(token => node(/^\d/.test(token) ? 'mn' : /^\s+$/.test(token) ? 'mtext'
          : /^[\p{L}\p{M}]$/u.test(token) ? 'mi' : 'mo', [token], attrs)));
      }
      case 'f': {
        const type = prop('type', 'bar');
        if (type === 'lin' || type === 'skw') return row([arg('num'), node('mo', ['/']), arg('den')]);
        return node('mfrac', [arg('num'), arg('den')], type === 'noBar' ? {linethickness:'0'} : {});
      }
      case 'sSub': return node('msub', [arg('e'), arg('sub')]);
      case 'sSup': return node('msup', [arg('e'), arg('sup')]);
      case 'sSubSup': return node('msubsup', [arg('e'), arg('sub'), arg('sup')]);
      case 'sPre': return node('mmultiscripts', [arg('e'), node('mprescripts'), arg('sub'), arg('sup')]);
      case 'rad': return enabled(prop('degHide', '0')) ? node('msqrt', [arg('e')]) : node('mroot', [arg('e'), arg('deg')]);
      case 'd': {
        const parts: Node[] = [];
        const delimiter = (s: string) => node('mo', [s], {stretchy:'true', fence:'true'});
        parts.push(delimiter(prop('begChr', '(')));
        children(e).filter(c => c.localName === 'e').forEach((c, i) => {
          if (i) parts.push(delimiter(prop('sepChr', '|')));
          parts.push(convert(c));
        });
        parts.push(delimiter(prop('endChr', ')')));
        return row(parts);
      }
      case 'nary': {
        const operator = node('mo', [prop('chr', '∫')], {largeop:'true', movablelimits:'false'});
        const sub = !enabled(prop('subHide', '0'));
        const sup = !enabled(prop('supHide', '0'));
        const under = prop('limLoc', 'subSup') === 'undOvr';
        let limits: Element = operator;
        if (sub && sup) limits = node(under ? 'munderover' : 'msubsup', [operator, arg('sub'), arg('sup')]);
        else if (sub) limits = node(under ? 'munder' : 'msub', [operator, arg('sub')]);
        else if (sup) limits = node(under ? 'mover' : 'msup', [operator, arg('sup')]);
        return row([limits, arg('e')]);
      }
      case 'm': return node('mtable', children(e).filter(c => c.localName === 'mr').map(c =>
        node('mtr', children(c).filter(p => p.localName === 'e').map(p => node('mtd', [convert(p)])))));
      case 'eqArr': return node('mtable', children(e).filter(c => c.localName === 'e').map(c => node('mtr', [node('mtd', [convert(c)])])));
      case 'limLow': return node('munder', [arg('e'), arg('lim')]);
      case 'limUpp': return node('mover', [arg('e'), arg('lim')]);
      case 'func': return row([arg('fName'), node('mo', ['\u2061']), arg('e')]);
      case 'acc': {
        const accents: Record<string, string> = {'\u0302':'^','\u0303':'~','\u0304':'¯','\u0307':'˙','\u0308':'¨','\u20d7':'→'};
        const mark = prop('chr', '\u0302');
        return node('mover', [arg('e'), node('mo', [accents[mark] ?? mark], {stretchy:'true'})], {accent:'true'});
      }
      case 'bar': return node(prop('pos', 'bot') === 'top' ? 'mover' : 'munder',
        [arg('e'), node('mo', ['¯'], {stretchy:'true'})]);
      case 'groupChr': {
        const top = prop('pos', 'bot') === 'top';
        const mark = node('mo', [prop('chr', '⏟')], {stretchy:'true'});
        return node(top ? 'mover' : 'munder', [arg('e'), mark]);
      }
      // Unsupported presentation constructs are visible as a whole-equation
      // notice, rather than silently discarding parts (e.g. phantom/strikeout).
      default: throw new Error('暂不支持公式结构 ' + name);
    }
  }
  const math = dom.documentElement;
  math.setAttribute('display', source.localName === 'oMathPara' ? 'block' : 'inline');
  math.append(convert(source));
  return math;
}

/** Keep Mammoth's semantic headings/links while restoring OMML at its original
 * position. Tokens exist only in a conversion copy, never in source downloads.
 */
export async function convertDocxWithMath(original: ArrayBuffer): Promise<{value: string; warnings: string[]}> {
  const zip = await JSZip.loadAsync(await docxSvgCompatibility(original));
  const prefix = 'VFMATH' + crypto.randomUUID().replaceAll('-', '') + 'X';
  const equations = new Map<string, Element>();
  const warnings: string[] = [];
  for (const name of ['word/document.xml', 'word/footnotes.xml', 'word/endnotes.xml', 'word/comments.xml']) {
    const file = zip.file(name);
    if (!file) continue;
    const xml = new DOMParser().parseFromString(await file.async('string'), 'application/xml');
    if (xml.querySelector('parsererror')) throw new Error('DOCX 正文 XML 无法解析');
    const roots = [...xml.getElementsByTagNameNS(OMML, '*')].filter(e =>
      (e.localName === 'oMathPara' || e.localName === 'oMath') && e.parentElement?.namespaceURI !== OMML);
    for (const source of roots) {
      const token = prefix + equations.size + 'END';
      let output: Element;
      try { output = equation(source); }
      catch (error) {
        const message = error instanceof Error ? error.message : '公式转换失败';
        warnings.push(message);
        output = document.createElement('span');
        output.setAttribute('class', 'document-math-warning');
        output.textContent = '[公式未能预览，请下载原件查看]';
      }
      equations.set(token, output);
      const run = xml.createElementNS(WORD, 'w:r');
      const text = xml.createElementNS(WORD, 'w:t');
      text.textContent = token; run.append(text); source.replaceWith(run);
    }
    if (roots.length) zip.file(name, new XMLSerializer().serializeToString(xml));
  }
  const result = await mammoth.convertToHtml({arrayBuffer: await zip.generateAsync({type:'arraybuffer'})}, {
    // Pandoc's fenced code paragraphs must retain preformatted semantics.
    styleMap: ["p[style-name='Source Code'] => pre:fresh > code"],
  });
  const parsed = new DOMParser().parseFromString(result.value, 'text/html');
  const walker = parsed.createTreeWalker(parsed.body, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  while (walker.nextNode()) texts.push(walker.currentNode as Text);
  const pattern = new RegExp(prefix + '\\d+END', 'g');
  const restored = new Set<string>();
  for (const text of texts) {
    const matches = [...text.data.matchAll(pattern)];
    if (!matches.length) continue;
    const fragment = parsed.createDocumentFragment(); let offset = 0;
    for (const match of matches) {
      fragment.append(text.data.slice(offset, match.index));
      const formula = equations.get(match[0]);
      if (!formula) throw new Error('DOCX 公式位置无法核验');
      fragment.append(parsed.importNode(formula, true)); restored.add(match[0]);
      offset = match.index + match[0].length;
    }
    fragment.append(text.data.slice(offset)); text.replaceWith(fragment);
  }
  if (restored.size !== equations.size) warnings.push('部分公式所在的文档区域未能预览，请下载原件查看');
  return {value: parsed.body.innerHTML, warnings: [...warnings, ...result.messages.map(m => m.message)]};
}
