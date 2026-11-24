interface Recognition {
  lang: string;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: { resultIndex: number; results: ArrayLike<{
    isFinal: boolean; 0?: { transcript: string };
  }> }) => void) | null;
  onerror: ((event: {error: string}) => void) | null;
  onend: (() => void) | null;
}

export type VoiceState = "starting" | "listening" | "processing" | "stopped" | `error:${string}`;
type OnState = (state: VoiceState) => void;
export interface VoiceSession { start(): void; stop(): void; cancel(): void }

export class VoiceController implements VoiceSession {
  private recognition: Recognition | null = null;
  private finishing = false;
  private factory: () => Recognition;
  private onText: (text: string) => void;
  private onState: OnState;

  constructor(factory: () => Recognition, onText: (text: string) => void, onState: OnState) {
    this.factory = factory;
    this.onText = onText;
    this.onState = onState;
  }

  start() {
    if (this.recognition) return;
    try {
      const selected = this.factory();
      this.recognition = selected;
      selected.lang = navigator.language || "zh-CN";
      selected.onresult = event => {
        if (this.recognition !== selected) return;
        const transcript = Array.from(event.results).slice(event.resultIndex)
          .filter(result => result.isFinal).map(result => result[0]?.transcript).join(" ").trim();
        if (transcript) this.onText(transcript);
      };
      selected.onerror = event => {
        if (this.recognition !== selected) return;
        this.cancel();
        this.onState(`error:${event.error}`);
      };
      selected.onend = () => {
        if (this.recognition !== selected) return;
        this.recognition = null;
        this.finishing = false;
        this.onState("stopped");
      };
      selected.start();
      this.onState("listening");
    } catch {
      this.cancel();
      this.onState("error:audio-capture");
    }
  }

  stop() {
    if (this.finishing) {
      this.cancel();
      this.onState("stopped");
    } else if (this.recognition) {
      this.finishing = true;
      this.onState("processing");
      try { this.recognition.stop(); } catch {
        this.cancel();
        this.onState("error:audio-capture");
      }
    }
  }

  cancel() {
    const selected = this.recognition;
    this.recognition = null;
    this.finishing = false;
    if (selected) {
      selected.onerror = selected.onend = selected.onresult = null;
      try { selected.abort(); } catch { /* Already ended. */ }
    }
  }
}

const SAMPLE_RATE = 16_000;

async function wave(recorded: Blob, maxSeconds: number) {
  const reader = new AudioContext();
  let decoded: AudioBuffer;
  try { decoded = await reader.decodeAudioData(await recorded.arrayBuffer()); }
  finally { await reader.close(); }
  const length = Math.min(maxSeconds * SAMPLE_RATE, Math.ceil(decoded.duration * SAMPLE_RATE));
  if (length < 2000) throw new Error("录音太短，请重新录音");
  const resampling = new OfflineAudioContext(1, length, SAMPLE_RATE);
  const source = resampling.createBufferSource();
  source.buffer = decoded;
  source.connect(resampling.destination);
  source.start();
  const mono = (await resampling.startRendering()).getChannelData(0);
  const audio = new ArrayBuffer(44 + length * 2);
  const pcm = new DataView(audio);
  pcm.setUint32(0, 0x52494646);
  pcm.setUint32(4, audio.byteLength - 8, true);
  pcm.setUint32(8, 0x57415645);
  pcm.setUint32(12, 0x666d7420);
  pcm.setUint32(16, 16, true);
  pcm.setUint16(20, 1, true);
  pcm.setUint16(22, 1, true);
  pcm.setUint32(24, SAMPLE_RATE, true);
  pcm.setUint32(28, SAMPLE_RATE * 2, true);
  pcm.setUint16(32, 2, true);
  pcm.setUint16(34, 16, true);
  pcm.setUint32(36, 0x64617461);
  pcm.setUint32(40, length * 2, true);
  for (let index = 0; index < mono.length; index++) {
    pcm.setInt16(44 + index * 2, Math.max(-32768, Math.min(32767, mono[index] * 32768)), true);
  }
  return new Blob([audio], { type: "audio/wav" });
}

export class RecordedVoice implements VoiceSession {
  private stream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private deadline: number | undefined;
  private controller = new AbortController();
  private maxSeconds: number;
  private transcribe: (audio: Blob, signal: AbortSignal) => Promise<string>;
  private onText: (text: string) => void;
  private onState: OnState;

  constructor(maxSeconds: number, transcribe: (audio: Blob, signal: AbortSignal) => Promise<string>,
              onText: (text: string) => void, onState: OnState) {
    this.maxSeconds = maxSeconds;
    this.transcribe = transcribe;
    this.onText = onText;
    this.onState = onState;
  }

  start() { void this.record(); }

  private async record() {
    const signal = this.controller.signal;
    this.onState("starting");
    try {
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
        throw new Error("此浏览器无法录音，请检查安全连接和麦克风权限");
      }
      const stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1}});
      if (signal.aborted) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
      this.stream = stream;
      const recorder = new MediaRecorder(stream);
      this.recorder = recorder;
      const chunks: Blob[] = [];
      recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
      recorder.onerror = () => {
        this.cancel();
        this.onState("error:audio-capture");
      };
      recorder.onstop = () => void this.finish(new Blob(chunks, {type: recorder.mimeType}));
      recorder.start();
      this.deadline = window.setTimeout(() => this.stop(), this.maxSeconds * 1000);
      this.onState("listening");
    } catch (error) {
      if (signal.aborted) return;
      this.cancel();
      const reason = error instanceof DOMException
        ? error.name === "NotAllowedError" ? "not-allowed" : "audio-capture"
        : error instanceof Error ? error.message : "audio-capture";
      this.onState(`error:${reason}`);
    }
  }

  private release() {
    window.clearTimeout(this.deadline);
    this.stream?.getTracks().forEach(track => track.stop());
    this.stream = null;
    this.recorder = null;
  }

  private async finish(clip: Blob) {
    const signal = this.controller.signal;
    this.release();
    if (signal.aborted) return;
    this.onState("processing");
    try {
      if (!clip.size) throw new Error("no-speech");
      const audio = await wave(clip, this.maxSeconds);
      if (signal.aborted) return;
      const result = await this.transcribe(audio, signal);
      if (signal.aborted) return;
      this.onText(result);
      this.onState("stopped");
    } catch (error) {
      if (!signal.aborted) this.onState(`error:${error instanceof Error ? error.message : "audio-capture"}`);
    }
  }

  stop() {
    if (this.recorder?.state === "recording") {
      this.recorder.stop();
      this.onState("processing");
    } else {
      this.cancel();
      this.onState("stopped");
    }
  }

  cancel() {
    this.controller.abort();
    if (this.recorder) {
      this.recorder.onstop = this.recorder.ondataavailable = this.recorder.onerror = null;
      if (this.recorder.state === "recording") this.recorder.stop();
    }
    this.release();
  }
}

export function voiceError(reason: string) {
  if (reason === "not-allowed" || reason === "service-not-allowed") return "请允许麦克风访问后重试。";
  if (reason === "audio-capture") return "无法使用麦克风，请检查设备。";
  if (reason === "no-speech") return "未检测到语音，请重新录音。";
  if (reason === "network") return "语音识别服务连接失败，请重试。";
  return reason;
}
