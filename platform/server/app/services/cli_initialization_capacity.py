"""Native initialization credit bound to the durable CLI owner and selection.

Unknown startup/stop outcomes retain the whole credit. No lease or TTL release.
"""
import asyncio,json
from sqlalchemy import select
from ..config import get_settings
from ..models import CliInitializationReservation,NativeCliSession,User
from . import codex_records as records
from .codex_wsl_state import initialization_payload
from .durable_events import json_text,now
from .storage_admission import lock_owner
from .async_ops import finish_before_cancel

LAUNCH_ORDER='registered_bridge_before_initializer_release_v1'


def binding(identifier,selected):
    records.profile(selected)
    if selected['runtime']!='wsl' or selected.get('native_state') is None:
        raise ValueError('Native initialization requires a frozen native selection')
    from .. import storage
    return json_text({'schema':'vibeflow.cli-initialization-capacity.v3','session_id':identifier,
        'user_id':selected['user_id'],'selection_sha256':records.digest(selected),'node_root':str(storage._fs_root().resolve()),
        'launch_order':LAUNCH_ORDER})


async def reserve(db,owner,selected,*,inventory=None):
    if selected['runtime']!='wsl':return
    size=sum(map(len,initialization_payload(selected['native_state'])))
    from .quota import check_locked_storage_quota,check_storage_quota
    user=await db.get(User,selected['user_id'])
    if user is None or user.status!='active':raise ValueError('Native initialization owner is unavailable')
    if inventory is None:
        await check_storage_quota(db,user,size)
    else:
        await check_locked_storage_quota(db,user,size,inventory=inventory)
    db.add(CliInitializationReservation(session_id=owner.id,user_id=user.id,
        binding_json=binding(owner.id,selected),size=size,remaining_bytes=size,state='active',receipt_json='{}'))
    await db.flush()


def validate(credit,session):
    selected=json.loads(session.profile_json)
    expected=json.loads(binding(session.id,selected));actual=json.loads(credit.binding_json)
    if actual.get('schema') in {'vibeflow.cli-initialization-capacity.v1','vibeflow.cli-initialization-capacity.v2'}:
        expected.pop('launch_order');expected['schema']=actual['schema']
        if actual['schema']=='vibeflow.cli-initialization-capacity.v1':expected.pop('node_root')
    if (credit.user_id!=session.created_by or actual!=expected
            or credit.size!=sum(map(len,initialization_payload(selected['native_state'])))):
        raise ValueError('Native initialization capacity binding changed')
    settings=get_settings()
    from .runtime_config import get_runtime_setting
    if (selected['state_root']!=settings.codex_wsl_state_root
            or selected['distro']!=get_runtime_setting('wsl_distro','vibeflow-linux')):
        raise ValueError('Native initialization inventory no longer covers its selected root')
    return selected


def settle(credit,status,proof):
    if credit.state=='settled':return
    if credit.state!='active' or credit.remaining_bytes!=credit.size or credit.receipt_json!='{}':
        raise ValueError('Native initialization capacity state changed')
    credit.state='settled';credit.remaining_bytes=0
    credit.receipt_json=json_text({'schema':'vibeflow.cli-initialization-receipt.v1','status':status,**proof})
    credit.updated_at=now()


async def permit_initializer(db,owner,transport):
    """Commit a registered bridge with active credit before sending its permit."""
    session=await db.get(NativeCliSession,owner.id)
    await lock_owner(db,session.created_by)
    credit=await db.get(CliInitializationReservation,owner.id,populate_existing=True)
    if credit is None or credit.state!='active' or credit.remaining_bytes!=credit.size or credit.receipt_json!='{}':
        raise ValueError('Native initialization no longer owns active capacity')
    validate(credit,session)
    journal,_=records.document(session)
    if journal['root']!=transport.owned.record or journal['bridge']!=transport.bridge_birth:
        raise ValueError('Native initialization permit differs from its registered bridge')


async def prepared(db,owner,transport):
    await lock_owner(db,json.loads((await db.get(NativeCliSession,owner.id)).profile_json)['user_id'])
    await records.verify_prepared(db,owner,transport)
    session=await db.get(NativeCliSession,owner.id,populate_existing=True)
    credit=await db.get(CliInitializationReservation,owner.id,populate_existing=True)
    if credit is None:raise ValueError('Native initialization has no committed capacity')
    validate(credit,session)
    settle(credit,'prepared',{'profile_sha256':transport.profile_sha256,
        'bridge_sha256':records.digest(transport.bridge_birth)})


async def reconcile(db,user_id):
    """Caller owns the quota transaction lock; terminal proof is mandatory."""
    credits=await db.scalars(select(CliInitializationReservation).where(
        CliInitializationReservation.user_id==user_id,CliInitializationReservation.state=='active')
        .order_by(CliInitializationReservation.created_at).limit(100))
    for credit in credits:
        session=await db.get(NativeCliSession,credit.session_id,populate_existing=True)
        if session is None or session.phase not in {'closed','failed'}:continue
        try:
            validate(credit,session)
            journal,stop=records.document(session)
            scope=journal.get('management',{}).get('retirement')
            if scope is None and (stop is None or not stop['verified']):continue
        except (ValueError,KeyError,TypeError,AttributeError):continue
        if scope is not None:
            settle(credit,'verified_scope',{'scope_sha256':records.digest(scope)})
        else:
            settle(credit,'verified_stop',{'stop_sha256':records.digest(stop)})
    await db.flush()
    from .cli_node_capacity import pending,release
    for identifier in await finish_before_cancel(asyncio.to_thread(pending,user_id)):
        credit=await db.get(CliInitializationReservation,identifier,populate_existing=True)
        session=await db.get(NativeCliSession,identifier,populate_existing=True)
        if credit is None or session is None or credit.state!='settled' or credit.user_id!=user_id:continue
        try:
            selected=settlement_ticket(credit,session)
            if selected is not None:await finish_before_cancel(asyncio.to_thread(release,selected,allow_fragments=True))
        except (ValueError,KeyError,TypeError,AttributeError,OSError):continue


def settlement_ticket(credit,session):
    validate(credit,session)
    if credit.state!='settled' or credit.remaining_bytes!=0:raise ValueError('Native credit is not settled')
    journal,stop=records.document(session)
    receipt=json.loads(credit.receipt_json)
    if receipt.get('schema')!='vibeflow.cli-initialization-receipt.v1':raise ValueError('Invalid native settlement receipt')
    if receipt.get('status')=='prepared':
        profile=json.loads(session.profile_json)
        if (journal['bridge'] is None or receipt.get('bridge_sha256')!=records.digest(journal['bridge'])
                or receipt.get('profile_sha256')!=profile['transport']['profile_sha256']):
            raise ValueError('Native preparation receipt binding changed')
    elif receipt.get('status')=='verified_stop':
        if stop is None or not stop['verified'] or receipt.get('stop_sha256')!=records.digest(stop):
            raise ValueError('Native stop receipt binding changed')
    elif receipt.get('status')=='verified_scope':
        scope=journal.get('management',{}).get('retirement')
        if scope is None or receipt.get('scope_sha256')!=records.digest(scope):
            raise ValueError('Native scope settlement differs from its actual closure')
    elif receipt.get('status') in {'initializer_exited','initializer_not_released'}:
        from .cli_initialization_recovery import validate_receipt
        validate_receipt(credit,session)
    else:raise ValueError('Unknown native settlement receipt')
    return ticket(credit,session)


def ticket(credit,session):
    bound=json.loads(credit.binding_json)
    if bound.get('schema')=='vibeflow.cli-initialization-capacity.v1':return None
    return {'node_root':bound['node_root'],'session_id':session.id,'user_id':credit.user_id,
        'selection_sha256':bound['selection_sha256'],'bytes':credit.size,'owner':json.loads(session.owner_identity_json)}


async def acquire_node(owner):
    from ..db import SessionLocal
    from .cli_node_capacity import acquire
    async with SessionLocal() as db:
        session=await records.current(db,owner,launch=True)
        credit=await db.get(CliInitializationReservation,owner.id)
        if credit is None or credit.state!='active':raise ValueError('Native node admission has no active user credit')
        validate(credit,session);selected=ticket(credit,session)
        if selected is None:raise ValueError('Native node admission requires current capacity binding')
    # No SQL transaction is held during the filesystem inventory/claim write.
    await finish_before_cancel(asyncio.to_thread(acquire,selected))


async def closed(user_id):
    from ..db import SessionLocal
    from .async_ops import finish_before_cancel
    async with SessionLocal() as db:
        await lock_owner(db,user_id,wait=True,settlement=True)
        await reconcile(db,user_id)
        await finish_before_cancel(db.commit())
