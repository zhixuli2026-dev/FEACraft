"""Use verified same-case postprocess versions as historical references."""
import hashlib
import json
import uuid

from sqlalchemy import select

from ..models import ArtifactPublication, Job, JobAttempt
from .attempt_workspace import checked_path


def _requests(config):
    for metric, reference in config.get("referenceValues", {}).items():
        if "previousRun" not in reference:
            continue
        selection = reference["previousRun"]
        if (not isinstance(selection, dict) or set(selection) != {"publicationId", "metric"}
                or not isinstance(selection["metric"], str) or not selection["metric"].strip()
                or "value" in reference or reference.get("kind", "regression") != "regression"):
            raise ValueError("A previous-run reference needs a publication and metric, with kind regression")
        identifier = selection["publicationId"]
        if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
            raise ValueError("Previous-run publication ID must be a canonical UUID")
        yield metric, reference, identifier, selection["metric"]


def _metrics_path(job_id):
    return f"D-post/data/postprocess-{job_id}/metrics.json"


def bound_metrics_paths(config):
    paths = set()
    for reference in config.get("referenceValues", {}).values():
        if "previousRun" not in reference:
            continue
        provenance = reference.get("provenance")
        job_id = provenance.get("job_id") if isinstance(provenance, dict) else None
        if type(job_id) is not int or job_id <= 0:
            raise ValueError("Historical reference was not bound to a sealed job")
        paths.add(_metrics_path(job_id))
    return sorted(paths)


async def previous_selections(db, case, config):
    identifiers = sorted({identifier for _, _, identifier, _ in _requests(config)})
    if not identifiers:
        return []
    if len(identifiers) > 16:
        raise ValueError("Publication input selections cannot exceed 16 versions")
    # Use full entities so a detached publication read set can replay these
    # ownership and lineage checks in the submission transaction.
    query = select(ArtifactPublication).where(
        ArtifactPublication.id.in_(identifiers), ArtifactPublication.case_id == case.id,
        ArtifactPublication.kind == "postprocess", ArtifactPublication.status.in_(("committed", "superseded")))
    jobs = {}
    for row in await db.scalars(query.order_by(ArtifactPublication.id)):
        job = await db.scalar(select(Job).join(JobAttempt, JobAttempt.job_id == Job.id)
            .where(JobAttempt.id == row.attempt_id, Job.case_id == case.id, Job.status == "succeeded"))
        if job is not None:
            jobs[row.id] = job.id
    if set(jobs) != set(identifiers):
        raise ValueError("The historical postprocess version is unavailable for this case")
    return [{"publication_id": identifier, "paths": [_metrics_path(jobs[identifier])], "require_current": False}
            for identifier in identifiers]


def _read_metrics(root, manifest, selected):
    paths = selected.get("paths")
    if (selected.get("kind") != "postprocess" or selected.get("require_current") is not False
            or not isinstance(paths, list) or len(paths) != 1):
        raise ValueError("Historical reference has no sealed postprocess metric input")
    raw = checked_path(root, paths[0]).read_bytes()
    metrics = json.loads(raw)
    if not isinstance(metrics, dict):
        raise ValueError("Historical metrics are not a document")
    job_id = metrics.get("job_id")
    if (metrics.get("schema") != "vibeflow.postprocess.metrics.v1"
            or metrics.get("case_id") != manifest["source"]["case_id"] or type(job_id) is not int or job_id <= 0
            or paths[0] != _metrics_path(job_id)):
        raise ValueError("Historical metrics do not match their sealed publication")
    rows = metrics.get("metrics")
    if (not isinstance(rows, list) or any(not isinstance(item, dict)
            or not isinstance(item.get("name"), str) or "value" not in item for item in rows)):
        raise ValueError("Historical metrics are not a named list")
    values = {item["name"]: item for item in rows}
    if len(values) != len(rows):
        raise ValueError("The historical postprocess version contains duplicate metrics")
    return job_id, values, hashlib.sha256(raw).hexdigest()


def bind_previous_references(root, manifest, config):
    selections = {item["publication_id"]: item for item in manifest.get("publications", [])}
    cached = {}
    for _, reference, identifier, metric_name in _requests(config):
        selected = selections.get(identifier)
        if selected is None:
            raise ValueError("Previous-run references must use an immutable publication input")
        if identifier not in cached:
            cached[identifier] = _read_metrics(root, manifest, selected)
        job_id, metrics, artifact_sha256 = cached[identifier]
        metric = metrics.get(metric_name)
        if metric is None:
            raise ValueError("The historical publication does not contain the selected metric")
        unit = metric.get("unit")
        if not unit or unit == "unknown" or reference.get("unit", unit) != unit:
            raise ValueError("Previous-run metric units do not match the reference")
        value, sampling = metric["value"], metric.get("sampling")
        if isinstance(value, list) and not sampling:
            raise ValueError("Previous-run arrays must record their sampling axes")
        reference.update(value=value, unit=unit, kind="regression", sampling=sampling,
                         source=f"Historical postprocess publication {identifier}",
                         provenance={
                             "publication_id": identifier, "manifest_sha256": selected["manifest_sha256"],
                             "metrics_sha256": artifact_sha256, "job_id": job_id, "metric": metric_name,
                         })
