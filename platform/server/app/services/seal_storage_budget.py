"""Conservative retained-byte peak for input sealing and its recovery records.

This is user storage admission, not a node-cache or native-runtime disk quota.
"""
from dataclasses import dataclass
import json
from .. import storage
from .durable_events import json_text


@dataclass(frozen=True)
class Estimate:
    user_bytes: int
    payload_bytes: int
    manifest_bytes: int
    plan_bytes: int
    recovery_bytes: int
    installation_bytes: int


def estimate(case,attempt,snapshot,*,manifest=None):
    from . import attempt_seal_journal as seals,attempt_plan_writes as writes
    remote=storage._use_s3()
    if manifest is None:
        selected=snapshot.local if remote else snapshot
        manifest=selected.describe()
    sizes=[row['size'] for row in manifest['files'].values()]
    if any(type(size) is not int or size<0 for size in sizes):raise ValueError('Invalid seal file size')
    payload=sum(sizes)*(1 if remote else 2)
    encoded=(json_text(manifest) if remote else json.dumps(manifest,ensure_ascii=False,sort_keys=True)).encode('utf8')
    if remote and len(encoded)>16*1024*1024:raise ValueError('S3 attempt manifest exceeds 16 MiB')
    plan=seals.describe_plan(case,attempt,manifest)
    plan_encoded=json_text(plan).encode('utf8')
    recovery=max(len(json_text({'plan':plan,'outcome':state}).encode('utf8')) for state in ('committed','rolled_back'))
    if max(len(plan_encoded),recovery)>seals.MAX_RECORD:raise ValueError('Seal recovery metadata exceeds its record limit')
    installation=0
    if not remote:
        # Both installation paths have equal UUID widths. Reserve the larger
        # history record plus its temporary hard-link name and owner intent.
        record={'schema':writes.SCHEMA,'id':'0'*32,'binding':seals.binding(case.s3_prefix),
            'target':seals.PREFIX+'/history/'+attempt.id+'.json','owner':{},
            'file':{'size':recovery,'sha256':'0'*64}}
        # Recovery may execute in another process/platform, so reserve its
        # enforced owner-record envelope instead of this process's PID width.
        installation=len(json_text(record).encode('utf8'))-2+writes.MAX_OWNER_BYTES
    total=payload+len(encoded)+len(plan_encoded)+recovery*(1 if remote else 2)+installation
    return Estimate(total,payload,len(encoded),len(plan_encoded),recovery,installation)
