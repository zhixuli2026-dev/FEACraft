import { useEffect, useMemo, useState } from 'react';

export interface ImageAttachment { name: string; mime: string; path?: string; data?: string; size?: number }
export const maxMessageImages = 3;

export async function readImageFiles(files: File[], existing: number): Promise<ImageAttachment[]> {
  if (files.length + existing > maxMessageImages) throw new Error('每条消息最多 3 张图片');
  const allowed = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
  for (const file of files) {
    if (!allowed.has(file.type)) throw new Error(`仅支持 PNG/JPEG/GIF/WEBP：${file.name}`);
    if (!file.size || file.size > 4 * 1024 * 1024) throw new Error(`图片须大于 0 且不超过 4 MiB：${file.name}`);
    if (file.name.length > 255) throw new Error('图片文件名不能超过 255 个字符');
  }
  return Promise.all(files.map(async file => {
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1] ?? '');
      reader.onerror = () => reject(new Error(`图片读取失败：${file.name}`));
      reader.onabort = () => reject(new Error('图片读取已取消'));
      reader.readAsDataURL(file);
    });
    return { name: file.name, mime: file.type, data };
  }));
}

export function DraftImages({ images, onRemove, disabled }: {
  images: ImageAttachment[]; onRemove: (index: number) => void; disabled?: boolean;
}) {
  if (!images.length) return null;
  return <div className="agent-draft-images" aria-label="待发送图片">
    {images.map((image, index) => <div key={index} className="agent-draft-image" data-testid="attachment-chip">
      {image.data && <img src={`data:${image.mime};base64,${image.data}`} alt={image.name} />}
      <span title={image.name}>{image.name}</span>
      <button type="button" disabled={disabled} data-testid="attachment-remove" aria-label={`移除图片 ${image.name}`}
        onClick={() => onRemove(index)}>移除</button>
    </div>)}
  </div>;
}

export default function ChatImages({ images, caseId }: { images: ImageAttachment[]; caseId: number }) {
  const [urls, setUrls] = useState<Record<number, string>>({});
  const identity = useMemo(() => JSON.stringify(images), [images]);
  useEffect(() => {
    const abort = new AbortController();
    const created: string[] = [];
    setUrls({});
    void (async () => {
      const values = JSON.parse(identity) as ImageAttachment[];
      for (const [index, image] of values.entries()) {
        if (image.data) {
          if (!abort.signal.aborted) setUrls(current => ({ ...current, [index]: `data:${image.mime};base64,${image.data}` }));
        } else if (image.path) {
          try {
            const token = localStorage.getItem('vf_token');
            const response = await fetch(`/api/v1/cases/${caseId}/raw?path=${encodeURIComponent(image.path)}`, {
              signal: abort.signal, headers: token ? { Authorization: `Bearer ${token}` } : {},
            });
            if (!response.ok) continue;
            const blob = await response.blob();
            if (abort.signal.aborted) return;
            const url = URL.createObjectURL(blob);
            created.push(url); setUrls(current => ({ ...current, [index]: url }));
          } catch { /* Keep the filename when the file is unavailable. */ }
        }
      }
    })();
    return () => { abort.abort(); created.forEach(url => URL.revokeObjectURL(url)); };
  }, [identity, caseId]);
  return <>{images.map((image, index) => urls[index] ?
    <img key={index} data-testid="chat-image" src={urls[index]} alt={image.name}
      style={{ display: 'block', maxWidth: 'min(220px, 100%)', maxHeight: 160, objectFit: 'contain', marginTop: 5, borderRadius: 6, border: '1px solid var(--vf-border)' }} /> :
    <span key={index} style={{ fontSize: 12, color: 'var(--vf-muted)' }}>{image.name}</span>)}</>;
}
