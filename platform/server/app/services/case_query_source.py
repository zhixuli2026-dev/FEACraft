"""Attach actual native reads to a complete, still-retained sealed source."""
import hashlib,json
from pathlib import Path
from . import case_file_queries as queries
from .case_read_view import delta_source


def _remote_snapshot(prefix):
    from .s3_case_files import snapshot

    return snapshot(prefix, catalog_only=True, windows_modes=False)


def attach(descriptor,native):
    tracking=descriptor['configuration'].get('case_queries')
    if tracking is None:return  # Historical native receipts keep their original contract.
    observed=native.get('case_queries')
    if (tracking.get('schema')!=queries.SCHEMA or tracking.get('helper_sha256')!=hashlib.sha256(Path(queries.__file__).read_bytes()).hexdigest()
            or not isinstance(observed,dict) or set(observed)!={'source','evidence'}):
        raise ValueError('Native case query provenance differs from its admitted helper')
    source=descriptor.get('origin_source',descriptor['source']);expected=queries.projection(source,observed['source']['policy'])
    if expected!=observed['source']:raise ValueError('Native query inputs differ from the complete sealed source')
    evidence=observed['evidence'];records=evidence['records']
    if (not 1<=len(records)<=1024 or sum(r['size'] for r in records)!=evidence['bytes'] or evidence['bytes']>queries.MAX_BYTES
            or not 0<evidence['events']<=500000 or sum(r['events'] for r in records)<evidence['events']
            or evidence.get('external_dependency_closure') is not False):
        raise ValueError('Native case query evidence is missing or exceeds its bounds')
    for record in records:
        if (not record['path'].startswith('.vibeflow/case-query-') or len(record['sha256'])!=64
                or type(record['size']) is not int or record['size']<0):raise ValueError('Invalid native query record')
    # This is deliberately additional provenance. The original complete source
    # remains bound by the immutable native configuration and invocation receipt.
    descriptor['origin_source']=source
    descriptor['source']=expected


def current(prefix,descriptor,*,pinned_paths=(),retired_aliases=None):
    origin=descriptor['origin_source']
    deleted=dict(descriptor.get('deleted_files',{}))
    for path,record in (retired_aliases or {}).items():
        before=origin['files'].get(path)
        if before is not None and all(before[k]==record[k] for k in ['size','sha256']):deleted[path]=before
    restored=_remote_snapshot(prefix)
    if restored is None:restored=delta_source(prefix,{**descriptor,'source':origin,'deleted_files':deleted})
    else:
        from .case_file_delta import restore_source,restore_event_prefix,parents
        restored=restore_event_prefix(prefix,{**descriptor,'source':origin},
            restore_source(restored,{**descriptor,'source':origin,'deleted_files':deleted}))
        restored['directories']=sorted(parents(restored['files']))
    # Mounted immutable inputs are revalidated through input_publications. Their
    # mutable compatibility aliases never replace the actual mounted records.
    from .case_file_delta import parents
    for path in pinned_paths:restored['files'][path]=origin['files'][path]
    restored['directories']=sorted(set(restored['directories'])|parents(pinned_paths))
    return queries.projection(restored,descriptor['source']['policy'])
