import { useEffect, useRef, useState } from "react";
import { api } from "../api";

export interface CaseDictSnapshot {
  content: string;
  content_sha256: string;
  dict_revision: number;
  parsed: Record<string, any> | null;
  workflow_state: string;
  parse_status: string;
  workflow_defaults?: {solve_mode?: 'serial' | 'mpi' | 'udf'; subdomains?: number; udf_name?: string; placement?: 'remote'};
  workflow_defaults_error?: string;
  interactive_defaults?: {shell: 'cmd' | 'wsl' | 'wsl-sandbox'};
}
export interface DictReceipt { dict_revision: number; content_sha256: string }

/** A draft keeps its source version through panel refreshes and in-flight saves. */
export function useCaseDraft<T>(snapshot: CaseDictSnapshot, source: T, key: string) {
  const fresh = () => ({value:source, baseline:snapshot as DictReceipt, dirty:false, sequence:0, key});
  const [draft, setDraft] = useState(fresh);
  const current = useRef(draft);
  const [review, setReview] = useState<CaseDictSnapshot | null>(null);
  const reviewGeneration = useRef(0);
  const update = (next: typeof draft) => { current.current=next; setDraft(next); };
  useEffect(() => {
    if (current.current.key !== key) {
      reviewGeneration.current++;
      setReview(null);
      update(fresh());
    } else if (!current.current.dirty && current.current.baseline.content_sha256 !== snapshot.content_sha256) {
      update({...current.current, value:source, baseline:snapshot});
    }
  }, [snapshot, key]);

  const edit = (value: T) => update({...current.current, value, dirty:true, sequence:current.current.sequence+1});
  const capture = () => {
    const value = current.current;
    if (value.key !== key || !value.baseline.content_sha256) throw new Error("无法确认表单版本，请重新读取后再保存。");
    return {...value, conditions:{expected_revision:value.baseline.dict_revision,
      expected_sha256:value.baseline.content_sha256}};
  };
  const accept = (submitted: ReturnType<typeof capture>, receipt: DictReceipt) => {
    if (submitted.key !== current.current.key) return;
    reviewGeneration.current++;
    update({...current.current, baseline:receipt, dirty:current.current.sequence !== submitted.sequence});
    setReview(null);
  };
  const inspect = async (caseId: number) => {
    const target = current.current.key;
    const generation = ++reviewGeneration.current;
    const latest = await api.get<CaseDictSnapshot>(`/cases/${caseId}/caseDict`);
    if (current.current.key === target && generation === reviewGeneration.current) setReview(latest);
  };
  const resolve = (value: T, keepDraft: boolean) => {
    if (!review) return;
    reviewGeneration.current++;
    update({...current.current, baseline:review, value:keepDraft ? current.current.value : value,
      dirty:keepDraft, sequence:current.current.sequence+1});
    setReview(null);
  };
  return {draft, edit, capture, accept, inspect, review, resolve, isDirty:() => current.current.dirty};
}
