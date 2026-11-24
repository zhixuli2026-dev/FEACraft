"""Compare the pinned native postprocessing publications of a workflow batch."""

import hashlib
import json
import math
import re

from fastapi import HTTPException
from sqlalchemy import select

from .. import storage
from ..models import User, WorkflowRun, WorkflowStep
from .case_boundary_inputs import scalar
from .case_read_capacity import reading
from .case_write import CaseWriteConflict, bounded_case_read
from .casedict import CASE_DICT_PATH, parse_casedict
from .file_read import ArchivedPublication
from .job_execution import request_hash
from .postprocess_artifacts import LATEST
from .postprocess_publication import select_result
from .publication_result_reads import Selection, archive_json
from .workflow_state import ExecutionConflict


def normalize(study, members):
    if study is None:
        return None
    path = study['parameter_path']
    if (not isinstance(path,str) or len(path)>160 or not all(re.fullmatch('[A-Za-z_][A-Za-z0-9_-]*',part)
             for part in path.split('.'))):
        raise ExecutionConflict('invalid_request','Study parameter must name a scalar caseDict entry')
    if len(members)<2 or any(member['target'] not in ('postprocess','report') for member in members):
        raise ExecutionConflict('invalid_request','A study requires at least two postprocessing workflows')
    mode = study['mode']
    goal = study.get('goal')
    weights = study.get('weights')
    if (mode == 'optimization') != (goal in ('minimize','maximize')):
        raise ExecutionConflict('invalid_request','An optimization study requires a minimization or maximization goal')
    if weights is not None and mode != 'UQ':
        raise ExecutionConflict('invalid_request','Study weights require UQ mode')
    if mode == 'UQ':
        keys = [member['key'] for member in members]
        values = weights if weights is not None else dict.fromkeys(keys,1.)
        if set(values) != set(keys) or any(not math.isfinite(value) or value<0 for value in values.values()):
            raise ExecutionConflict('invalid_request','UQ requires a finite nonnegative weight for every member')
        scale = max(values.values())
        if not scale:
            raise ExecutionConflict('invalid_request','UQ requires a positive total weight')
        scaled = {key:values[key]/scale for key in keys}
        total = math.fsum(scaled.values())
        study = {**study,'weights':{key:value/total for key,value in scaled.items()}}
    return study


def bind_parameter(case, run, study):
    data = storage.get_bytes(case.s3_prefix,CASE_DICT_PATH)
    admitted = json.loads(run.input_manifest_json)
    if hashlib.sha256(data).hexdigest() != admitted['case_dict_sha256']:
        raise ExecutionConflict('input_stale','Study parameter source changed during batch submission')
    value = parse_casedict(data.decode('utf-8'))
    try:
        for part in study['parameter_path'].split('.'):
            parent = value
            value = value[part]
        if part == 'value' and isinstance(parent,dict) and 'unit' in parent and parent['unit']!=study['parameter_unit']:
            raise ValueError('Recorded parameter unit differs from the study unit')
        if isinstance(value,dict):
            if value.get('unit') != study['parameter_unit']:
                raise ValueError('Recorded parameter unit differs from the study unit')
            value = value['value']
        return scalar(value)
    except (KeyError,TypeError,ValueError) as error:
        raise ExecutionConflict('invalid_request','Study parameter value or unit cannot be verified in caseDict') from error


def _read_metric(selection, study):
    with ArchivedPublication(selection.prefix,selection.publication) as archive:
        pointer = archive_json(archive,LATEST)
        path = pointer['metrics_path']
        metrics = archive_json(archive,path)
        matched = [item for item in metrics['metrics'] if item['name']==study['metric']]
        if len(matched)!=1 or matched[0]['unit']!=study['metric_unit']:
            raise ValueError('Study response metric is missing, ambiguous, or has another unit')
        return scalar(matched[0]['value']), path, archive.manifest['files'][path]['sha256']


def _summary(study, samples):
    values = [row['metric_value'] for row in samples]
    if study['mode'] == 'optimization':
        optimum = (min if study['goal']=='minimize' else max)(values)
        return {'optimum':optimum,'member_keys':[row['key'] for row in samples if row['metric_value']==optimum]}
    if study['mode'] == 'UQ':
        weights = [study['weights'][row['key']] for row in samples]
        scale = max(map(abs,values)) or 1.
        scaled = [value/scale for value in values]
        mean = math.fsum(weight*value for weight,value in zip(weights,scaled))
        variance = math.fsum(weight*(value-mean)**2 for weight,value in zip(weights,scaled))
        return {'mean':scale*mean,'population_standard_deviation':scale*math.sqrt(variance)}
    return {'minimum':min(values),'maximum':max(values)}


async def results(db, batch_id, user):
    from ..routers.workspaces import get_owned_case
    from .workflow_batches import owned, roster

    actor_id = user.id
    batch,_ = await owned(db,batch_id,user)
    study = json.loads(batch.request_json).get('study')
    if study is None:
        raise HTTPException(404,'此批次没有参数研究配置')
    _, members = await roster(db,batch)
    # Each remote read releases its SQL snapshot. Keep the admitted facts as
    # values across reads so later members never access expired ORM attributes.
    runs = {run.id:run for run in await db.execute(select(WorkflowRun.id,WorkflowRun.case_id,WorkflowRun.created_by,
            WorkflowRun.request_hash,WorkflowRun.input_manifest_json,WorkflowRun.status)
            .where(WorkflowRun.id.in_([member['run_id'] for member in members])))}
    if any(run.status!='succeeded' for run in runs.values()):
        raise ExecutionConflict('study_not_ready','Study results require successful confirmed workflows for every member')
    samples = []
    for member in members:
        try:
            run = runs[member['run_id']]
            if (run.case_id!=member['case_id'] or run.created_by!=actor_id or run.request_hash!=member['request_hash']
                    or request_hash(json.loads(run.input_manifest_json))!=member['input_sha256']):
                raise ValueError('Study member differs from its admitted workflow')
            step = (await db.execute(select(WorkflowStep.output_manifest_json,WorkflowStep.status,WorkflowStep.active_attempt_id)
                    .where(WorkflowStep.run_id==run.id,WorkflowStep.step_key=='postprocess'))).one_or_none()
            publication = json.loads(step.output_manifest_json)['publication']['id']
            parameter = scalar(member['parameter_value'])
            actor = await db.get(User,actor_id,populate_existing=True)
            if actor is None:
                raise HTTPException(404,'工作流批次不存在')
            case = await get_owned_case(run.case_id,actor,db,write=False)
            async with reading(db,case):
                selection = await select_result(db,case,publication,None)
                if not isinstance(selection,Selection) or step.status!='succeeded' or selection.publication.attempt_id!=step.active_attempt_id:
                    raise ValueError('Study response does not belong to the completed postprocess step')
                value,path,digest = await bounded_case_read(_read_metric,selection,study,code='workflow_study',label='研究指标读取')
        except CaseWriteConflict:
            raise
        except (AttributeError,OSError,ValueError,TypeError,KeyError) as error:
            raise ExecutionConflict('evidence_invalid','A study member has no verifiable response at its recorded version') from error
        samples.append({'key':member['key'],'case_id':member['case_id'],'run_id':member['run_id'],
                        'parameter_value':parameter,'metric_value':value,'publication_id':publication,
                        'metrics_path':path,'metrics_sha256':digest})
    return {'batch_id':batch_id,'study':study,'samples':samples,'summary':_summary(study,samples)}
