import { parseDelimited } from "./delimited";

self.onmessage = ({ data }: MessageEvent<{ content: string; delimiter: string }>) => {
  self.postMessage(parseDelimited(data.content, data.delimiter));
};
