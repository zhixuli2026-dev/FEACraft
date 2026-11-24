"""Recover remote file journals while a SQL reservation excludes content writers."""
from . import s3_file_transactions as remote,storage_operations as operations
from .s3_staging_transactions import writer_state
from .case_write import CaseWriteConflict
from .durable_events import append_event,json_text


async def recover_reserved(lease, *, committed_document=None):
    from botocore.exceptions import ClientError,BotoCoreError
    try:return await _recover_reserved(lease, committed_document=committed_document)
    except (TimeoutError, CaseWriteConflict):raise
    except (ValueError,KeyError,TypeError,AttributeError,OSError,ClientError,BotoCoreError) as error:
        raise remote.journal._conflict('S3 recovery could not verify its evidence; preserve the operation for review') from error


async def _recover_reserved(lease, *, committed_document):
    operations.require_context(lease)
    case=lease.case
    if committed_document is None:
        from .s3_live_store import fence
        await remote._io(fence, case.s3_prefix)
    elif not lease.transfers_drained:
        raise remote.journal._conflict('Committed storage writer has not drained')
    document,etag=await remote._io(remote._load,case,verify_blobs=False)
    if committed_document is not None and document != committed_document:
        raise remote.journal._conflict('Committed storage journal differs from the local write')
    if document is None:
        lease.resolved=True
        return None
    if not await lease.has_drain_proof(document):
        if committed_document is not None:
            raise remote.journal._conflict('Committed storage journal differs from the active writer')
        state=await remote._io(writer_state,case,document)
        if state in {'active','unverified'}:raise operations.busy()
    verified,pending_etag=await remote._io(remote._load,case)
    if verified!=document or etag!=pending_etag:
        raise CaseWriteConflict('file_recovery_required','待恢复文件清单在校验期间变化')
    async with lease.control(authorize=False) as db:
        event=await remote.journal._witness(db,case,document)
    if committed_document is not None and event is None:
        raise remote.journal._conflict('Committed storage journal has no SQL witness')
    plan=document['plan'];directory=remote._directory(plan['id'])
    await remote._io(remote.live.require_conditions,case.s3_prefix)
    observed=[]
    for entry in plan['files']:
        data,_,metadata=await remote._io(remote.snapshot,case.s3_prefix,entry['path'],limit=remote.limits(plan.get('scope','files'))[1])
        before=remote._matches(data,metadata,entry,'before')
        after=remote._matches(data,metadata,entry,'after')
        if not (after if event is not None else before or after):
            raise remote.journal._conflict('Pending S3 transaction conflicts with current bytes; preserve it for review',entry['path'])
        observed.append(before)
    if event is None:
        for index,(entry,before) in enumerate(zip(plan['files'],observed)):
            if not before:
                async with lease.control(authorize=False):pass
                await remote._io(remote._apply,case,directory,index,entry,'before','after')
    receipt={'transaction_id':plan['id'],'decision':'committed' if event is not None else 'rolled_back',
             'event_id':document['event_id'],'plan_sha256':document['plan_sha256']}
    await remote._io(remote.objects.put_new,case.s3_prefix,directory+'/resolution.json',json_text(receipt).encode())
    await remote._io(remote._conditional,case.s3_prefix,remote.PENDING,None,pending_etag,None)
    lease.resolved=True
    async with lease.control(authorize=False) as db:
        await append_event(db,case.id,'storage.operation.recovered',receipt)
    return receipt


async def recover(checked):
    async with operations.hold(checked,purpose='recovery') as lease:
        return await recover_reserved(lease)
