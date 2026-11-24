import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import { jobRecoveryRequest } from '../utils/jobRecovery';
import { readLogCursor, saveLogCursor } from '../utils/viewSelection';
import './JobLogView.css';

interface Props { userId: number; caseId: number; jobId: number; attemptId: string | null }
interface LogPage {
  job_id: number; case_id: number; attempt_id: string | null; start_offset: number;
  offset: number; size: number; chunk: string; has_more: boolean; reset: boolean;
}
interface Slice { start: number; end: number; text: string }
const PAGE = 65536, DISPLAY = 1048576;
function retryWait(signal: AbortSignal) {
  return new Promise<void>(resolve => {
    const stop = () => { clearTimeout(timer); signal.removeEventListener('abort', stop); resolve(); };
    const timer = window.setTimeout(stop, 1000);
    signal.addEventListener('abort', stop, { once: true });
    if (signal.aborted) stop();
  });
}

/** The browser keeps byte positions; displayed text is re-read from the real log. */
export default function JobLogView({ userId, caseId, jobId, attemptId }: Props) {
  const [slices, setSlices] = useState<Slice[]>([]);
  const [position, setPosition] = useState(0);
  const [connection, setConnection] = useState('正在读取日志…');
  const [notice, setNotice] = useState('');
  const [following, setFollowing] = useState(true);
  const [olderBusy, setOlderBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const current = useRef<Slice[]>([]), scroll = useRef<HTMLDivElement>(null);
  const olderRequest = useRef<AbortController | null>(null), alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; olderRequest.current?.abort(); };
  }, []);
  function validate(value: LogPage) {
    if (value.job_id !== jobId || value.case_id !== caseId || value.attempt_id !== attemptId
      || !Number.isSafeInteger(value.offset) || !Number.isSafeInteger(value.start_offset)
      || value.start_offset < 0 || value.offset < value.start_offset || value.size < value.offset) {
      throw new Error('日志身份或位置无法核验。');
    }
  }
  function publish(value: Slice[], keepEarlier = false) {
    let length = value.reduce((total, item) => total + item.text.length, 0);
    while (length > DISPLAY && value.length > 1) length -= (keepEarlier ? value.pop()! : value.shift()!).text.length;
    current.current = value; setSlices(value);
  }
  useEffect(() => {
    if (!following) return;
    const control = new AbortController();
    let offset = readLogCursor(userId, caseId, jobId, attemptId), initialized = false;
    const acknowledge = (value: number) => {
      offset = value; setPosition(value); saveLogCursor(userId, caseId, jobId, attemptId, value);
    };
    setNotice(''); publish([]);
    void (async () => {
      while (!control.signal.aborted) {
        let stream: AbortController | null = null;
        try {
          if (!initialized) {
            const value = await jobRecoveryRequest<LogPage>(`/jobs/${jobId}/log?offset=${Math.max(0, offset - PAGE)}&limit=${PAGE}&end_offset=${offset}`, { signal: control.signal });
            if (control.signal.aborted) return;
            validate(value);
            publish(value.chunk ? [{ start: value.start_offset, end: value.offset, text: value.chunk }] : []);
            acknowledge(value.offset); initialized = true;
            if (value.reset) setNotice('日志位置已变化，已重新核对现存日志。');
          }
          stream = new AbortController();
          const abort = () => stream?.abort();
          control.signal.addEventListener('abort', abort, { once: true });
          try {
            const token = localStorage.getItem('vf_token');
            const response = await fetch(`/api/v1/jobs/${jobId}/log-stream?offset=${offset}`,
              { headers: token ? { Authorization: `Bearer ${token}` } : {}, signal: stream.signal });
            if (!response.ok) throw new ApiError(response.status, '日志连接失败');
            if (!response.body) throw new Error('日志连接没有返回内容。');
            setConnection('正在跟随最新输出');
            const reader = response.body.getReader(), decoder = new TextDecoder();
            let buffer = '';
            while (!control.signal.aborted) {
              const deadline = window.setTimeout(() => stream?.abort(), 15000);
              let data: ReadableStreamReadResult<Uint8Array>;
              try { data = await reader.read(); } finally { clearTimeout(deadline); }
              if (data.done) throw new Error('日志连接提前结束。');
              buffer += decoder.decode(data.value, { stream: true });
              const frames = buffer.split('\n\n'); buffer = frames.pop() ?? '';
              for (const frame of frames) {
                const line = frame.split('\n').find(item => item.startsWith('data:'));
                if (!line) continue;
                const event = JSON.parse(line.slice(5));
                if (event.type === 'error') {
                  if (event.code === 'log_missing') {
                    publish([]); setConnection('日志文件缺失，无法核对输出。'); return;
                  }
                  if (['permission_denied', 'state_conflict'].includes(event.code)) {
                    publish([]); setConnection('原日志已不可访问，已停止显示。'); return;
                  }
                  if (event.code === 'log_read_failed') throw new Error('日志读取中断。');
                  setConnection('作业收尾需要核验，已保留读取到的日志。'); return;
                }
                if (event.type === 'done') {
                  if (event.job_id !== jobId || event.case_id !== caseId || event.attempt_id !== attemptId) throw new Error('日志结束身份无法核验。');
                  setConnection('当前日志读取完成'); return;
                }
                if (event.type !== 'log') continue;
                const value: LogPage = { ...event, chunk: event.text }; validate(value);
                if (value.reset) { publish([]); acknowledge(value.start_offset); setNotice('日志位置已变化，已重新核对现存日志。'); }
                if (value.offset <= offset) continue;
                if (value.start_offset !== offset) throw new Error('日志位置不连续，正在重新连接。');
                publish([...current.current, { start: value.start_offset, end: value.offset, text: value.chunk }]);
                acknowledge(value.offset);
              }
            }
          } finally { control.signal.removeEventListener('abort', abort); stream.abort(); }
        } catch (failure) {
          if (control.signal.aborted) return;
          if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) {
            publish([]); setConnection('原日志已不可访问，已停止显示。'); return;
          }
          setConnection('日志连接中断，正在从已读位置重连。');
          await retryWait(control.signal);
        }
      }
    })();
    return () => control.abort();
  }, [userId, caseId, jobId, attemptId, following, revision]);
  useEffect(() => { if (following) scroll.current?.scrollTo(0, scroll.current.scrollHeight); }, [slices, following]);

  async function readEarlier() {
    const start = current.current[0]?.start;
    if (!start || olderBusy) return;
    setFollowing(false); setConnection('已暂停跟随，正在阅读较早日志。'); setOlderBusy(true);
    const controller = new AbortController(); olderRequest.current = controller;
    try {
      const value = await jobRecoveryRequest<LogPage>(`/jobs/${jobId}/log?offset=${Math.max(0, start - PAGE)}&limit=${PAGE}&end_offset=${start}`, { signal: controller.signal });
      if (!alive.current) return;
      validate(value);
      if (value.reset || value.offset !== start || current.current[0]?.start !== start) {
        setNotice('日志范围已变化，请重新核对后读取。'); return;
      }
      publish([{ start: value.start_offset, end: value.offset, text: value.chunk }, ...current.current], true);
      setNotice('');
    } catch (failure) {
      if (!controller.signal.aborted && alive.current) {
        if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) publish([]);
        setNotice('较早日志暂时无法读取，请重试或核对权限。');
      }
    } finally { if (alive.current) setOlderBusy(false); }
  }
  const start = slices[0]?.start ?? 0;
  return <div className="job-log-view" data-testid="job-log-view" data-log-offset={position} data-log-start={start}>
    <div className="job-log-tools">
      <span role="status">{connection}</span>
      {start > 0 && <button disabled={olderBusy} onClick={() => void readEarlier()}>{olderBusy ? '正在读取…' : '读取更早日志'}</button>}
      {!following && <button onClick={() => { setFollowing(true); setRevision(value => value + 1); }}>跟随最新输出</button>}
      <button onClick={() => { setFollowing(true); setRevision(value => value + 1); }}>重新核对</button>
    </div>
    {start > 0 && <p className="job-log-notice">当前显示部分日志，可读取更早内容。</p>}
    {notice && <p className="job-log-notice" role="status">{notice}</p>}
    <div className="job-log-scroll" ref={scroll}><pre>{slices.map(item => item.text).join('') || (connection === '当前日志读取完成' ? '（此作业没有日志输出）' : connection.includes('缺失') || connection.includes('不可访问') ? '（日志读取已停止）' : '（等待输出…）')}</pre></div>
  </div>;
}
