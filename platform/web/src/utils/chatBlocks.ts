export interface ChatBlock {
  key: string;
  text: string;
}

/** 按空行分段，但不切断围栏代码块；index key 在流式追加时保持稳定。 */
export function splitChatBlocks(source: string): ChatBlock[] {
  if (!source.trim()) return [];
  const blocks: ChatBlock[] = [];
  let current: string[] = [];
  let fence: string | null = null;
  const push = () => {
    const text = current.join("\n").trimEnd();
    if (text) blocks.push({ key: `block-${blocks.length}`, text });
    current = [];
  };
  for (const line of source.split(/\r?\n/)) {
    if (!fence && !line.trim()) {
      push();
      continue;
    }
    current.push(line);
    const candidate = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (!candidate) continue;
    const [, marker, suffix] = candidate;
    if (fence) {
      if (marker[0] === fence[0] && marker.length >= fence.length && /^[ \t]*$/.test(suffix)) {
        fence = null;
      }
    } else if (marker[0] === "~" || !suffix.includes("`")) {
      fence = marker;
    }
  }
  push();
  return blocks;
}
