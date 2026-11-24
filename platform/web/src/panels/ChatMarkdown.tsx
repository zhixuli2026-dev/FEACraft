import { memo, useMemo } from "react";
import { limitMarkdownSource, renderChatBlockHtml } from "../utils/chatRender";
import { splitChatBlocks } from "../utils/chatBlocks";

const ChatBlock = memo(function ChatBlock({ index, text }: { index: number; text: string }) {
  const rendered = useMemo(() => renderChatBlockHtml(text), [text]);
  if (rendered.kind === "plain") {
    return <pre data-testid="chat-code-degraded" style={{ whiteSpace: "pre-wrap", overflow: "auto" }}>{rendered.text}</pre>;
  }
  return <div data-testid="chat-block" data-block-index={index}
              dangerouslySetInnerHTML={{ __html: rendered.html }} />;
});

/** 流式仅更新变化的块；完成后从权威全文重建，并始终保持最多一个光标。 */
export default function ChatMarkdown({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const limited = useMemo(() => limitMarkdownSource(text), [text]);
  const blocks = useMemo(() => splitChatBlocks(limited.text), [limited.text]);
  return (
    <div className="chat-md" data-testid="chat-markdown" data-authoritative={!streaming}
         data-cursor-count={streaming ? 1 : 0} data-truncated={limited.truncated}
         key={streaming ? "stream" : "complete"}>
      {limited.truncated && (
        <div data-testid="chat-truncated" style={{ color: "#d8b86c", fontSize: 11 }}>
          仅显示末 512 KiB；完整消息仍保留在会话和复制中。
        </div>
      )}
      {blocks.map((block, index) => <ChatBlock key={block.key} index={index} text={block.text} />)}
      {streaming && <span data-testid="chat-cursor" style={{ color: "#3b82f6" }}>▍</span>}
    </div>
  );
}
