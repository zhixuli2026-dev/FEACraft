import { useRef, useState } from 'react';
export interface NativeApprovalValue { id: string; method: string; phase: string; params: Record<string, any> }
interface InputQuestion {
  id: string;
  header?: string;
  question: string;
  options?: { label: string; description?: string }[];
  isOther?: boolean;
  isSecret?: boolean;
}
const permissionLabels: Record<string, string> = { network: '网络访问', fileSystem: '文件系统访问' };

export default function NativeApproval({ value, readonly, respond }: {
  value: NativeApprovalValue; readonly: boolean; respond: (id: string, body: unknown) => Promise<void>;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [error, setError] = useState('');
  const [scope, setScope] = useState('turn');
  const [selected, setSelected] = useState<string[]>([]);
  const params = value.params;
  const input = value.method === 'item/tool/requestUserInput';
  const permissions = value.method === 'item/permissions/requestApproval';
  const requestedPermissions = permissions ? Object.entries(params.permissions ?? {}).filter(([, details]) => details != null) : [];
  const questions: InputQuestion[] = input ? params.questions ?? [] : [];
  const offered = params.availableDecisions ?? ['accept', 'acceptForSession', 'decline', 'cancel'];
  const chooseAnswer = (id: string, answer: string) => setAnswers(current => ({ ...current, [id]: answer }));
  const send = async (body: unknown) => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    // Secret values live only in this form until submission, never in drafts or receipts.
    if (input) setAnswers(current => Object.fromEntries(questions.filter(question => !question.isSecret)
      .map(question => [question.id, current[question.id] ?? ''])));
    try { await respond(value.id, body); }
    catch (e) { setError(String(e)); }
    finally { lock.current = false; setBusy(false); }
  };
  return <section className="native-approval" data-testid="native-approval" aria-label="原生交互请求">
    <strong>{input ? '需要你的回答' : permissions ? '请求额外权限' : '等待执行批准'}</strong>
    {params.reason && <p>{params.reason}</p>}
    {!input && <>
      {params.command && <pre>{params.command}</pre>}
      <details><summary>请求详情</summary><pre>{JSON.stringify(params, null, 2)}</pre></details>
    </>}
    {input ? <form onSubmit={event => {
      event.preventDefault();
      void send({ answers: Object.fromEntries(questions.map(question => [question.id, [answers[question.id] ?? '']])) });
    }}>
      {questions.map(question => {
        const id = `native-answer-${value.id}-${question.id}`;
        const answer = answers[question.id] ?? '';
        const options = question.isSecret ? [] : question.options ?? [];
        return <div className="native-question" key={question.id}>
          <label htmlFor={id}>{question.header ? `${question.header} · ` : ''}{question.question}</label>
          {options.length > 0 && !question.isOther ? <select id={id} required disabled={busy} value={answer}
            onChange={event => chooseAnswer(question.id, event.target.value)}>
            <option value="">请选择</option>
            {options.map(option => <option key={option.label} value={option.label}>
              {option.label}{option.description ? ` — ${option.description}` : ''}
            </option>)}
          </select> : <>
            {options.length > 0 && <div className="native-actions native-answer-options">
              {options.map(option => <button key={option.label} type="button" title={option.description}
                disabled={busy} aria-pressed={answer === option.label} onClick={() => chooseAnswer(question.id, option.label)}>
                {option.label}
              </button>)}
            </div>}
            <input id={id} required disabled={busy} maxLength={4096} type={question.isSecret ? 'password' : 'text'}
              autoComplete="off" spellCheck={!question.isSecret} value={answer}
              onChange={event => chooseAnswer(question.id, event.target.value)} />
          </>}
        </div>;
      })}
      <button disabled={busy}>提交回答</button>
    </form> : permissions ? <>
      {requestedPermissions.map(([key]) => <label className="native-check" key={key}>
        <input type="checkbox" disabled={readonly || busy} checked={selected.includes(key)} onChange={e =>
          setSelected(e.target.checked ? [...selected, key] : selected.filter(k => k !== key))} />
        {permissionLabels[key] ?? key}
      </label>)}
      <label className="native-question">授权范围<select value={scope} onChange={e => setScope(e.target.value)} disabled={busy || readonly}>
        <option value="turn">当前回合</option><option value="session">当前原生会话</option>
      </select></label>
      <div className="native-actions"><button disabled={busy || readonly || !selected.length} onClick={() => void send({
        permissions: Object.fromEntries(selected.map(k => [k, params.permissions[k]])), scope,
      })}>允许所选权限</button><button disabled={busy} onClick={() => void send({ permissions: {}, scope: 'turn' })}>拒绝权限</button></div>
    </> : <div className="native-actions">
      {(['accept', 'acceptForSession', 'decline', 'cancel'] as const).filter(d => offered.includes(d)).map(decision =>
        <button key={decision} disabled={busy || readonly && decision.startsWith('accept')} onClick={() => void send({ decision })}>
          {{ accept: '允许一次', acceptForSession: '本会话允许', decline: '拒绝', cancel: '取消请求' }[decision]}</button>)}
      {params.proposedExecpolicyAmendment && <button disabled={busy || readonly} onClick={() => void send({ decision: {
        acceptWithExecpolicyAmendment: { execpolicy_amendment: params.proposedExecpolicyAmendment },
      } })}>批准所示命令规则</button>}
      {params.proposedNetworkPolicyAmendments?.map((rule: any, index: number) => <button key={index} disabled={busy || readonly}
        onClick={() => void send({ decision: { applyNetworkPolicyAmendment: { network_policy_amendment: rule } } })}>
        应用网络规则：{rule.action} {rule.host}</button>)}
    </div>}
    {readonly && !input && <p>当前回合为只读。可拒绝请求，或在回合结束后切换权限。</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
