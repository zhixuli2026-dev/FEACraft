import JSZip from 'jszip';

const DRAWING = 'http://schemas.openxmlformats.org/drawingml/2006/main';
const RELATIONSHIPS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
const SVG = 'http://schemas.microsoft.com/office/drawing/2016/SVG/main';

/** Let the semantic reader consume Word's SVG-only picture representation.
 * The caller keeps the original Blob for downloads; only this conversion copy
 * is normalized. Existing raster fallbacks and relationships remain untouched.
 */
export async function docxSvgCompatibility(original: ArrayBuffer): Promise<ArrayBuffer> {
  const zip = await JSZip.loadAsync(original);
  let changed = false;
  for (const name of ['word/document.xml', 'word/footnotes.xml', 'word/endnotes.xml', 'word/comments.xml']) {
    const file = zip.file(name);
    if (!file) continue;
    const document = new DOMParser().parseFromString(await file.async('string'), 'application/xml');
    if (document.querySelector('parsererror')) throw new Error('DOCX 正文 XML 无法解析');
    let partChanged = false;
    for (const blip of document.getElementsByTagNameNS(DRAWING, 'blip')) {
      if (blip.hasAttributeNS(RELATIONSHIPS, 'embed') || blip.hasAttributeNS(RELATIONSHIPS, 'link')) continue;
      const svg = blip.getElementsByTagNameNS(SVG, 'svgBlip')[0];
      const relationship = svg?.getAttributeNS(RELATIONSHIPS, 'embed');
      if (!relationship) continue;
      blip.setAttributeNS(RELATIONSHIPS, 'r:embed', relationship);
      partChanged = true;
    }
    if (partChanged) {
      zip.file(name, new XMLSerializer().serializeToString(document));
      changed = true;
    }
  }
  return changed ? zip.generateAsync({type: 'arraybuffer'}) : original;
}
