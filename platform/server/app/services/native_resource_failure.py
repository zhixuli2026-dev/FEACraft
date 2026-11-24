"""Classify registered helper resource exits using actual bound stop receipts.

An arbitrary shell exit or a matching log string is never a resource receipt.
Native helpers turn tool failures into ordinary errors, reserving exit98 for
their provenance allowance. Cancellation and earlier failures take precedence.
"""
import json
from .case_file_queries import RESOURCE_EXIT


def classify(job,attempt,root_stop):
    from . import native_child_records,process_record,process_stop
    handler=json.loads(attempt.input_manifest_json).get('handler',{}).get('key')
    purposes={'mpi.openfoam':'mpi_run','solve.openfoam.mpi':'mpi_run',
        'udf.build':'udf_run','udf.run':'udf_run',
        'udf.build.workflow':'udf_run','solve.openfoam.udf':'udf_run'}
    if handler=='solve.openfoam.prepared':
        identity=process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt)
        stop=process_stop.validate(root_stop,identity)
        return 'resource_limit' if stop['verified'] and stop['root_exit_code']==RESOURCE_EXIT else ''
    if handler in purposes:
        children=native_child_records.document(job,attempt)['children'].values()
        if any(row['caller']=='worker' and row['purpose']==purposes[handler]
               and row['phase']=='stopped' and row['stop']['verified']
               and row['stop']['root_exit_code']==RESOURCE_EXIT for row in children):
            return 'resource_limit'
    return ''


async def for_owner(owner,root_stop):
    from ..db import SessionLocal
    from .job_execution import _owned
    async with SessionLocal() as db:
        job,attempt=await _owned(db,owner)
        return classify(job,attempt,root_stop)
