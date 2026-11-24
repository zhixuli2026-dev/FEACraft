"""Upgrade/rollback/constraints on disposable SQLite and optional local PostgreSQL.

--postgres creates a uniquely named temporary database via the local configured
role. It never migrates the configured database and drops only the database it
created. Connection strings/credentials are never included in reports.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from sqlalchemy import event, inspect, select, text,delete
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

SERVER = Path(__file__).resolve().parent
BASE = "6fb6c18fb7ee"
HEAD = "f9d4a10735bc"
NEW = {"workflow_batches", "workflow_batch_members", "cli_scope_allocations", "workflow_runs", "workflow_steps", "job_attempts", "job_execution_heads",
       "artifact_publications", "artifact_publication_heads", "case_event_counters",
       "durable_events", "event_outbox", "job_submission_requests", "native_requests", "native_sessions", "native_viz_sessions", "native_cli_sessions",
       "conversation_executions", "native_cli_turns", "native_cli_events", "native_cli_approvals", "native_cli_forks", "native_cli_session_requests",
       "website_oauth_transactions", "external_identities", "website_auth_sessions", "case_validation_fences", "case_storage_operations", "upload_storage_reservations", "publication_storage_reservations", "output_archive_reservations", "job_log_reservations", "storage_write_reservations", "owner_storage_reservations", "upload_chunk_credits", "cli_initialization_reservations"}


def engine_for(url):
    engine = create_async_engine(url)
    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def sqlite_fks(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def alembic(url, report, *arguments):
    env = dict(os.environ, VIBEFLOW_DATABASE_URL=url, VIBEFLOW_STORAGE_BACKEND="fs", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=SERVER,
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    with (report / "alembic.log").open("a", encoding="utf-8") as log:
        log.write("COMMAND " + " ".join(arguments) + "\n" + result.stdout + result.stderr + "\n")
    if result.returncode:
        raise AssertionError("Alembic command failed; inspect " + str(report / "alembic.log"))


async def legacy(url, *, seed=False):
    from app.models import Case, Job, Membership, Organization, User, Workspace
    engine = engine_for(url)
    try:
        async with engine.begin() as connection:
            if seed:
                await connection.execute(User.__table__.insert().values(id=101, email="migration@example.com",
                    name="legacy", password_hash="no-login"))
                await connection.execute(Organization.__table__.insert().values(id=101, name="migration organization"))
                await connection.execute(Membership.__table__.insert().values(org_id=101, user_id=101, role="admin"))
                await connection.execute(Workspace.__table__.insert().values(id=101, user_id=101, name="legacy"))
                await connection.execute(Case.__table__.insert().values(id=101, workspace_id=101,
                    name="legacy", s3_prefix="migration/legacy/"))
                await connection.execute(Job.__table__.insert().values(id=101, case_id=101, type="solve",
                    command="legacy command", status="succeeded", exit_code=0, log_s3_key="legacy.log",
                    parsed_summary='{"legacy":true}', ended_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
            row = (await connection.execute(select(Job.__table__).where(Job.id == 101))).mappings().one()
            return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()}
    finally:
        await engine.dispose()


async def schema_and_constraints(url):
    from app.db import Base
    from app.models import (ArtifactPublication, ArtifactPublicationHead, CaseEventCounter, DurableEvent,
        EventOutbox, Job, JobAttempt, JobExecutionHead, JobSubmissionRequest, WorkflowRun, WorkflowStep, NativeRequest, NativeSession, NativeVizSession, NativeCliSession,
        ConversationExecution, NativeCliTurn, NativeCliEvent, NativeCliApproval, Conversation,ToolCall,
        WebsiteOAuthTransaction, ExternalIdentity, WebsiteAuthSession, NativeCliFork, AgentEvent, NativeCliSessionRequest)
    engine = engine_for(url)
    checked = []
    try:
        async with engine.begin() as connection:
            actual = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
            assert actual == set(Base.metadata.tables) | {"alembic_version"}, actual
            assert (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == HEAD
            run = dict(id=str(uuid.uuid4()), case_id=101, org_id=101, created_by=101, target="report",
                request_hash="a"*64, idempotency_key="same-request", expected_revision=0, run_dir="C-run/test")
            await connection.execute(WorkflowRun.__table__.insert().values(**run))
            step = dict(id=str(uuid.uuid4()), run_id=run["id"], step_key="solve", handler="openfoam.solve")
            await connection.execute(WorkflowStep.__table__.insert().values(**step))
            attempt = dict(id=str(uuid.uuid4()), job_id=101, step_id=step["id"], number=1)
            await connection.execute(JobAttempt.__table__.insert().values(**attempt))
            assert (await connection.execute(select(JobAttempt.child_processes_json)
                .where(JobAttempt.id==attempt['id']))).scalar_one()=='{}'
            checked.append('child_execution_default_is_empty')
            assert (await connection.execute(select(JobAttempt.worker_identity_json)
                .where(JobAttempt.id==attempt['id']))).scalar_one()=='{}'
            checked.append('legacy_worker_identity_stays_unverified')
            await connection.execute(JobExecutionHead.__table__.insert().values(job_id=101, next_number=2,
                active_attempt_id=attempt["id"]))
            publication = dict(id=str(uuid.uuid4()), case_id=101, kind="report", generation=1,
                attempt_id=attempt["id"], immutable_path=".vibeflow/artifacts/report/1", manifest_sha256="b"*64)
            await connection.execute(ArtifactPublication.__table__.insert().values(**publication))
            await connection.execute(ArtifactPublicationHead.__table__.insert().values(case_id=101,
                kind="report", next_generation=2, active_publication_id=publication["id"]))
            await connection.execute(CaseEventCounter.__table__.insert().values(case_id=101, next_seq=2))
            ev = dict(id=str(uuid.uuid4()), case_id=101, seq=1, run_id=run["id"], step_id=step["id"],
                attempt_id=attempt["id"], type="workflow.created", payload_json='{"schema":"v1"}')
            await connection.execute(DurableEvent.__table__.insert().values(**ev))
            await connection.execute(EventOutbox.__table__.insert().values(event_id=ev["id"]))
            # A second Job separates request-family uniqueness from per-Job uniqueness.
            if not (await connection.execute(select(Job.id).where(Job.id == 102))).first():
                await connection.execute(Job.__table__.insert().values(id=102, case_id=101,
                    type="shell", command="constraint fixture"))
            submission = dict(id=str(uuid.uuid4()), case_id=101, created_by=101,
                idempotency_key="submission-key", request_hash="c"*64, job_id=101, execution_spec_json="{}")
            await connection.execute(JobSubmissionRequest.__table__.insert().values(**submission))
            checked.append("all_ten_models_roundtrip")

            async def rejects(label, model, values):
                try:
                    async with connection.begin_nested():
                        await connection.execute(model.__table__.insert().values(**values))
                    raise AssertionError("Constraint missing: " + label)
                except IntegrityError:
                    checked.append(label)

            from app.models import UploadStorageReservation
            from app.models import PublicationStorageReservation
            from app.models import OwnerStorageReservation
            owner_capacity=dict(id=str(uuid.uuid4()),user_id=101,binding_json='{}',writer_json='{}',size=100,remaining_bytes=100,state='active',receipt_json='{}')
            for label,override in (
                ('owner_capacity_negative_remaining',{'remaining_bytes':-1}),
                ('owner_capacity_overstated_remaining',{'remaining_bytes':101}),
                ('owner_capacity_negative_size',{'size':-1,'remaining_bytes':0}),
                ('owner_capacity_unknown_state',{'state':'invented'}),
                ('owner_capacity_settled_has_no_future_bytes',{'state':'settled','receipt_json':'{"closed":true}'}),
                ('owner_capacity_settled_requires_receipt',{'state':'settled','remaining_bytes':0}),
                ('owner_capacity_user_foreign_key',{'user_id':999})):
                await rejects(label,OwnerStorageReservation,{**owner_capacity,**override})
            await connection.execute(OwnerStorageReservation.__table__.insert().values(**{**owner_capacity,'remaining_bytes':50,'receipt_json':'{"closed":true}'}))
            checked.append('owner_capacity_retains_unknown_request_after_fence')
            await connection.execute(OwnerStorageReservation.__table__.update().values(state='settled',remaining_bytes=0))
            checked.append('owner_capacity_settled_with_fence_receipt')
            await connection.execute(delete(OwnerStorageReservation))
            from app.models import StorageWriteReservation,CaseStorageOperation
            operation_id=str(uuid.uuid4())
            await connection.execute(CaseStorageOperation.__table__.insert().values(id=operation_id,case_id=101,
                source_prefix='workspaces/101/cases/101',purpose='staging',state='active',owner_json='{}'))
            remote_capacity=dict(id=str(uuid.uuid4()),operation_id=operation_id,user_id=101,case_id=101,
                binding_json='{}',request_json='{}',writer_json='{}',size=100,remaining_bytes=100,state='active',receipt_json='{}')
            for label,override in (
                ('remote_write_negative_remaining',{'remaining_bytes':-1}),
                ('remote_write_overstated_remaining',{'remaining_bytes':101}),
                ('remote_write_active_keeps_full_bytes',{'remaining_bytes':99}),
                ('remote_write_negative_size',{'size':-1,'remaining_bytes':0}),
                ('remote_write_unknown_state',{'state':'invented'}),
                ('remote_write_active_has_no_receipt',{'receipt_json':'{"status":200}'}),
                ('remote_write_settled_has_no_future_bytes',{'state':'settled','receipt_json':'{"status":200}'}),
                ('remote_write_settled_requires_receipt',{'state':'settled','remaining_bytes':0}),
                ('remote_write_user_foreign_key',{'user_id':999}),
                ('remote_write_case_foreign_key',{'case_id':999}),
                ('remote_write_operation_foreign_key',{'operation_id':str(uuid.uuid4())}),
                ('remote_write_operation_required',{'operation_id':None})):
                await rejects(label,StorageWriteReservation,{**remote_capacity,**override})
            await connection.execute(StorageWriteReservation.__table__.insert().values(**{**remote_capacity,'size':0,'remaining_bytes':0}))
            checked.append('remote_write_empty_body_admitted')
            await connection.execute(StorageWriteReservation.__table__.update().values(state='settled',receipt_json='{"status":200}'))
            checked.append('remote_write_acknowledged_empty_body_settled')
            await connection.execute(delete(StorageWriteReservation))
            await connection.execute(delete(CaseStorageOperation).where(CaseStorageOperation.id==operation_id))
            from app.models import JobLogReservation
            log_capacity=dict(job_id=101,attempt_id=attempt['id'],user_id=101,case_id=101,
                binding_json='{}',sha256='a'*64,size=100,remaining_bytes=100,
                state='active',writer_json='{}',token=str(uuid.uuid4()))
            for label,override in (
                ('log_negative_remaining',{'remaining_bytes':-1}),
                ('log_overstated_remaining',{'remaining_bytes':101}),
                ('log_active_keeps_whole_copy',{'remaining_bytes':99}),
                ('log_negative_size',{'size':-1,'remaining_bytes':0}),
                ('log_closed_has_no_future_bytes',{'state':'materialized'}),
                ('log_unknown_state',{'state':'invented'}),
                ('log_user_foreign_key',{'user_id':999}),
                ('log_case_foreign_key',{'case_id':999}),
                ('log_job_foreign_key',{'job_id':999}),
                ('log_attempt_foreign_key',{'attempt_id':str(uuid.uuid4())})):
                await rejects(label,JobLogReservation,{**log_capacity,**override})
            from app.models import OutputArchiveReservation
            archive_capacity=dict(attempt_id=attempt['id'],user_id=101,case_id=101,
                binding_json='{}',input_sha256='b'*64,manifest_sha256='a'*64,size=100,remaining_bytes=100,
                state='active',writer_json='{}',token=str(uuid.uuid4()))
            for label,override in (
                ('output_archive_negative_remaining',{'remaining_bytes':-1}),
                ('output_archive_overstated_remaining',{'remaining_bytes':101}),
                ('output_archive_active_keeps_whole_copy',{'remaining_bytes':99}),
                ('output_archive_zero_size',{'size':0,'remaining_bytes':0}),
                ('output_archive_closed_has_no_future_bytes',{'state':'materialized'}),
                ('output_archive_unknown_state',{'state':'invented'}),
                ('output_archive_user_foreign_key',{'user_id':999}),
                ('output_archive_case_foreign_key',{'case_id':999}),
                ('output_archive_attempt_foreign_key',{'attempt_id':str(uuid.uuid4())})):
                await rejects(label,OutputArchiveReservation,{**archive_capacity,**override})
            publication_capacity=dict(publication_id=publication['id'],user_id=101,case_id=101,
                binding_json='{}',manifest_sha256='a'*64,size=100,remaining_bytes=100,
                state='active',writer_json='{}',token=str(uuid.uuid4()))
            for label,override in (
                ('publication_capacity_negative_remaining',{'remaining_bytes':-1}),
                ('publication_capacity_overstated_remaining',{'remaining_bytes':101}),
                ('publication_capacity_active_keeps_whole_copy',{'remaining_bytes':99}),
                ('publication_capacity_zero_size',{'size':0,'remaining_bytes':0}),
                ('publication_capacity_closed_has_no_future_bytes',{'state':'materialized'}),
                ('publication_capacity_unknown_state',{'state':'invented'}),
                ('publication_capacity_user_foreign_key',{'user_id':999}),
                ('publication_capacity_case_foreign_key',{'case_id':999}),
                ('publication_capacity_publication_foreign_key',{'publication_id':str(uuid.uuid4())})):
                await rejects(label,PublicationStorageReservation,{**publication_capacity,**override})
            reservation=dict(id=uuid.uuid4().hex,user_id=101,case_id=101,binding_json='{}',
                path='Ref/fixture.bin',size=100,remaining_bytes=100,state='active')
            for label,override in (
                ('upload_reservation_negative_remaining',{'remaining_bytes':-1}),
                ('upload_reservation_overstated_remaining',{'remaining_bytes':101}),
                ('upload_reservation_closed_has_no_future_bytes',{'state':'completed'}),
                ('upload_reservation_unknown_state',{'state':'invented'}),
                ('upload_reservation_user_foreign_key',{'user_id':999}),
                ('upload_reservation_case_foreign_key',{'case_id':999})):
                await rejects(label,UploadStorageReservation,{**reservation,**override})

            await rejects("idempotency_scope_unique", WorkflowRun, dict(run, id=str(uuid.uuid4())))
            await rejects("run_case_foreign_key", WorkflowRun, dict(run, id=str(uuid.uuid4()), case_id=999))
            await rejects("run_org_foreign_key", WorkflowRun, dict(run, id=str(uuid.uuid4()), org_id=999, idempotency_key=None))
            await rejects("invalid_run_state", WorkflowRun, dict(run, id=str(uuid.uuid4()), idempotency_key=None, status="invented"))
            await rejects("negative_version", WorkflowRun, dict(run, id=str(uuid.uuid4()), idempotency_key=None, version=-1))
            await rejects("step_key_unique_per_run", WorkflowStep, dict(step, id=str(uuid.uuid4())))
            await rejects("invalid_confirmation_state", WorkflowStep, dict(step, id=str(uuid.uuid4()), step_key="bad", confirmation_state="A"))
            await rejects("attempt_number_unique_per_job", JobAttempt, dict(attempt, id=str(uuid.uuid4())))
            await rejects("attempt_job_foreign_key", JobAttempt, dict(attempt, id=str(uuid.uuid4()), job_id=999))
            await rejects("attempt_step_foreign_key", JobAttempt, dict(attempt, id=str(uuid.uuid4()), step_id="missing", number=2))
            await rejects("attempt_number_positive", JobAttempt, dict(attempt, id=str(uuid.uuid4()), number=0))
            await rejects("attempt_root_foreign_key", JobAttempt, dict(attempt, id=str(uuid.uuid4()), number=2, root_job_id=999))
            await rejects("attempt_number_unique_across_retry_jobs", JobAttempt,
                dict(attempt, id=str(uuid.uuid4()), job_id=102, root_job_id=101))
            await rejects("retry_attempt_foreign_key", JobAttempt,
                dict(attempt, id=str(uuid.uuid4()), number=2, retry_of_attempt_id="missing"))
            await rejects("submission_key_unique", JobSubmissionRequest, dict(submission, id=str(uuid.uuid4()), job_id=102))
            await rejects("submission_job_unique", JobSubmissionRequest,
                dict(submission, id=str(uuid.uuid4()), idempotency_key=None))
            await rejects("submission_user_foreign_key", JobSubmissionRequest,
                dict(submission, id=str(uuid.uuid4()), idempotency_key=None, created_by=999, job_id=102))
            await rejects("publication_generation_unique", ArtifactPublication, dict(publication, id=str(uuid.uuid4())))
            await rejects("publication_attempt_foreign_key", ArtifactPublication, dict(publication, id=str(uuid.uuid4()), generation=2, attempt_id="missing"))
            await rejects("publication_head_foreign_key", ArtifactPublicationHead,
                dict(case_id=101, kind="bad", active_publication_id="missing"))
            await rejects("event_sequence_unique", DurableEvent, dict(ev, id=str(uuid.uuid4())))
            await rejects("event_sequence_positive", DurableEvent, dict(ev, id=str(uuid.uuid4()), seq=0))
            await rejects("outbox_event_unique", EventOutbox, dict(event_id=ev["id"]))
            await rejects("outbox_event_foreign_key", EventOutbox, dict(event_id="missing"))
            request=dict(id=str(uuid.uuid4()),case_id=101,created_by=101,owner_id='migration-owner',
                owner_token_sha256='d'*64,owner_identity_json='{}',launch_nonce='e'*64,
                kind='report.prepare',profile_json='{}',heartbeat_at=datetime.now(timezone.utc),
                lease_expires_at=datetime.now(timezone.utc))
            await connection.execute(NativeRequest.__table__.insert().values(**request))
            await rejects('native_request_case_foreign_key',NativeRequest,dict(request,id=str(uuid.uuid4()),case_id=999,launch_nonce='f'*64))
            await rejects('native_request_user_foreign_key',NativeRequest,dict(request,id=str(uuid.uuid4()),created_by=999,launch_nonce='f'*64))
            await rejects('native_request_nonce_unique',NativeRequest,dict(request,id=str(uuid.uuid4())))
            await rejects('native_request_invalid_kind',NativeRequest,dict(request,id=str(uuid.uuid4()),kind='shell',launch_nonce='f'*64))
            await rejects('native_request_invalid_phase',NativeRequest,dict(request,id=str(uuid.uuid4()),phase='unknown',launch_nonce='f'*64))
            await rejects('native_request_completion_requires_end',NativeRequest,dict(request,id=str(uuid.uuid4()),phase='succeeded',launch_nonce='f'*64))
            await rejects('native_request_active_has_no_end',NativeRequest,dict(request,id=str(uuid.uuid4()),ended_at=datetime.now(timezone.utc),launch_nonce='f'*64))
            synctex_id=str(uuid.uuid4())
            await connection.execute(NativeRequest.__table__.insert().values(**dict(request,id=synctex_id,launch_nonce='f'*64,kind='synctex.query')))
            await connection.execute(delete(NativeRequest).where(NativeRequest.id==synctex_id))
            checked.append('synctex_request_kind_allowed')
            await rejects('document_request_requires_case',NativeRequest,dict(request,id=str(uuid.uuid4()),case_id=None,launch_nonce='f'*64))
            await rejects('global_request_rejects_case',NativeRequest,dict(request,id=str(uuid.uuid4()),kind='runtime.wsl_probe',launch_nonce='f'*64))
            await rejects('global_request_user_foreign_key',NativeRequest,dict(request,id=str(uuid.uuid4()),case_id=None,created_by=999,kind='runtime.wsl_probe',launch_nonce='f'*64))
            for kind in ('runtime.wsl_probe','runtime.python_probe'):
                global_id=str(uuid.uuid4())
                await connection.execute(NativeRequest.__table__.insert().values(**dict(request,id=global_id,case_id=None,launch_nonce='f'*64,kind=kind)))
                await connection.execute(delete(NativeRequest).where(NativeRequest.id==global_id))
                checked.append(kind+'_without_case_allowed')
            # NULL keys must permit independent non-idempotent requests.
            for _ in range(2):
                await connection.execute(WorkflowRun.__table__.insert().values(**dict(run,
                    id=str(uuid.uuid4()), idempotency_key=None)))
            checked.append("null_idempotency_keys_do_not_conflict")
            session=dict(id=str(uuid.uuid4()),case_id=101,created_by=101,owner_id='migration-terminal',owner_token_sha256='1'*64,
                owner_identity_json='{}',launch_nonce='2'*64,shell='cmd',profile_json='{}',
                heartbeat_at=datetime.now(timezone.utc),lease_expires_at=datetime.now(timezone.utc))
            await connection.execute(NativeSession.__table__.insert().values(**session))
            for label,patch in (
                ('native_session_case_foreign_key',{'case_id':999}),('native_session_user_foreign_key',{'created_by':999}),
                ('native_session_invalid_shell',{'shell':'sh'}),('sandbox_session_requires_case',{'shell':'wsl-sandbox','case_id':None}),
                ('native_session_invalid_phase',{'phase':'invented'}),('native_session_negative_version',{'version':-1}),
                ('native_session_starting_has_no_end',{'ended_at':datetime.now(timezone.utc)}),
                ('native_session_active_has_no_end',{'phase':'active','ended_at':datetime.now(timezone.utc)}),
                ('native_session_closed_requires_end',{'phase':'closed'}),('native_session_failed_requires_end',{'phase':'failed'})):
                await rejects(label,NativeSession,dict(session,id=str(uuid.uuid4()),launch_nonce='3'*64,**patch))
            await rejects('native_session_nonce_unique',NativeSession,dict(session,id=str(uuid.uuid4())))
            for shell in ('cmd','wsl'):
                identifier=str(uuid.uuid4())
                await connection.execute(NativeSession.__table__.insert().values(**dict(session,id=identifier,case_id=None,shell=shell,launch_nonce='3'*64)))
                await connection.execute(delete(NativeSession).where(NativeSession.id==identifier))
                checked.append('global_terminal_'+shell+'_allowed')
            viz=dict(case_id=101,created_by=101,owner_id='migration-viz',owner_token_sha256='4'*64,
                owner_identity_json='{}',launch_nonce='5'*64,profile_json='{}',
                heartbeat_at=datetime.now(timezone.utc),lease_expires_at=datetime.now(timezone.utc),last_activity_at=datetime.now(timezone.utc))
            await connection.execute(NativeVizSession.__table__.insert().values(**viz))
            for label,patch in (
                ('native_viz_case_foreign_key',{'case_id':999}),('native_viz_user_foreign_key',{'created_by':999}),
                ('native_viz_requires_case',{'case_id':None}),('native_viz_invalid_phase',{'phase':'invented'}),
                ('native_viz_negative_version',{'version':-1}),('native_viz_starting_has_no_end',{'ended_at':datetime.now(timezone.utc)}),
                ('native_viz_active_has_no_end',{'phase':'active','ended_at':datetime.now(timezone.utc)}),
                ('native_viz_closed_requires_end',{'phase':'closed'}),('native_viz_failed_requires_end',{'phase':'failed'})):
                await rejects(label,NativeVizSession,dict(viz,launch_nonce='6'*64,**patch))
            await rejects('native_viz_nonce_unique',NativeVizSession,viz)
            if not (await connection.execute(select(Conversation.id).where(Conversation.id==101))).first():
                await connection.execute(Conversation.__table__.insert().values(id=101,case_id=101,title='migration Agent'))
            if not (await connection.execute(select(ToolCall.id).where(ToolCall.id=='migration-agent-shell'))).first():
                await connection.execute(ToolCall.__table__.insert().values(id='migration-agent-shell',conversation_id=101,tool='run_shell_command',status='running'))
            shell=dict(request,id=str(uuid.uuid4()),kind='agent.shell',conversation_id=101,tool_call_id='migration-agent-shell',launch_nonce='7'*64)
            await connection.execute(NativeRequest.__table__.insert().values(**shell))
            for index,(label,patch) in enumerate((
                ('agent_shell_requires_case',{'case_id':None}),('agent_shell_requires_conversation',{'conversation_id':None}),
                ('agent_shell_requires_tool',{'tool_call_id':None}),('agent_shell_conversation_fk',{'conversation_id':999}),
                ('agent_shell_tool_fk',{'tool_call_id':'missing-tool'}),('document_rejects_tool_binding',{'kind':'report.prepare'}),
                ('agent_tool_binding_unique',{}))):
                tid='migration-agent-'+str(index)
                if not (await connection.execute(select(ToolCall.id).where(ToolCall.id==tid))).first():
                    await connection.execute(ToolCall.__table__.insert().values(id=tid,conversation_id=101,tool='run_shell_command',status='running'))
                values=dict(shell,id=str(uuid.uuid4()),launch_nonce='8'*64,tool_call_id=tid if patch or label!='agent_tool_binding_unique' else shell['tool_call_id'])
                values.update(patch)
                await rejects(label,NativeRequest,values)
            cli=dict(id=str(uuid.uuid4()),case_id=101,conversation_id=101,created_by=101,owner_id='migration-cli',
                owner_token_sha256='a'*64,owner_identity_json='{}',launch_nonce='9'*64,runtime='windows',profile_json='{}',
                heartbeat_at=datetime.now(timezone.utc),lease_expires_at=datetime.now(timezone.utc))
            await connection.execute(NativeCliSession.__table__.insert().values(**cli))
            for label,patch in (
                ('cli_case_fk',{'case_id':999}),('cli_conversation_fk',{'conversation_id':999}),('cli_user_fk',{'created_by':999}),
                ('cli_conversation_requires_case',{'case_id':None}),('cli_runtime_invalid',{'runtime':'linux'}),
                ('cli_phase_invalid',{'phase':'invented'}),('cli_version_negative',{'version':-1}),
                ('cli_starting_has_no_end',{'ended_at':datetime.now(timezone.utc)}),
                ('cli_active_has_no_end',{'phase':'active','ended_at':datetime.now(timezone.utc)}),
                ('cli_closed_requires_end',{'phase':'closed'}),('cli_failed_requires_end',{'phase':'failed'}),
                ('cli_thread_state_invalid',{'thread_state':'invented'}),('cli_thread_ready_requires_id',{'thread_state':'ready'}),
                ('cli_thread_pending_rejects_id',{'thread_id':'t1'}),('cli_thread_id_nonempty',{'thread_state':'ready','thread_id':''})):
                await rejects(label,NativeCliSession,dict(cli,id=str(uuid.uuid4()),launch_nonce='a'*64,**patch))
            await rejects('cli_nonce_unique',NativeCliSession,dict(cli,id=str(uuid.uuid4())))
            global_id=str(uuid.uuid4())
            await connection.execute(NativeCliSession.__table__.insert().values(**dict(cli,id=global_id,case_id=None,conversation_id=None,launch_nonce='b'*64)))
            await connection.execute(delete(NativeCliSession).where(NativeCliSession.id==global_id))
            checked.append('cli_global_account_session_allowed')
            await connection.execute(NativeCliSession.__table__.insert().values(**dict(cli,
                id=str(uuid.uuid4()),runtime='wsl',launch_nonce='f'*64)))
            checked.append('cli_wsl_session_allowed')
            startup=dict(id=cli['id'],case_id=101,conversation_id=101,created_by=101,
                client_request_id='migration-startup',request_sha256='e'*64,request_json='{}',selection_json='{}')
            for label,patch in (
                ('case_fk',{'case_id':999}),('conversation_fk',{'conversation_id':999}),('user_fk',{'created_by':999}),
                ('session_fk',{'session_id':'missing'}),('phase',{'phase':'invented'}),('version',{'version':-1}),
                ('empty_client',{'client_request_id':''}),('hash',{'request_sha256':'x'}),
                ('completed_session',{'phase':'completed','ended_at':datetime.now(timezone.utc)}),
                ('session_binding',{'session_id':cli['id']}),('not_started_active',{'not_started':True}),
                ('not_started_session',{'not_started':True,'session_id':cli['id'],'phase':'failed','ended_at':datetime.now(timezone.utc)}),
                ('ended_active',{'ended_at':datetime.now(timezone.utc)}),('ended_terminal',{'phase':'failed'})):
                values=dict(startup,id=str(uuid.uuid4()),client_request_id=label);values.update(patch)
                if label=='session_fk':values['id']='missing'
                if label=='not_started_session':values['id']=cli['id']
                await rejects('cli_startup_'+label,NativeCliSessionRequest,values)
            await connection.execute(NativeCliSessionRequest.__table__.insert().values(**startup))
            await rejects('cli_startup_request_unique',NativeCliSessionRequest,dict(startup,id=str(uuid.uuid4())))
            checked.append('cli_startup_admission_before_child_allowed')
            turn=dict(id=str(uuid.uuid4()),session_id=cli['id'],case_id=101,conversation_id=101,created_by=101,
                client_message_id='migration-native',request_sha256='c'*64,mode='message')
            await connection.execute(NativeCliTurn.__table__.insert().values(**turn))
            for label,patch in (
                ('cli_turn_session_fk',{'session_id':'missing'}),('cli_turn_case_fk',{'case_id':999}),
                ('cli_turn_conversation_fk',{'conversation_id':999}),('cli_turn_user_fk',{'created_by':999}),
                ('cli_turn_invalid_mode',{'mode':'invented'}),('cli_turn_invalid_phase',{'phase':'invented'}),
                ('cli_turn_negative_version',{'version':-1}),('cli_turn_zero_sequence',{'next_seq':0}),
                ('cli_turn_negative_bytes',{'event_bytes':-1}),('cli_turn_requires_thread',{'native_turn_id':'n1'}),
                ('cli_turn_native_status_invalid',{'native_status':'invented'}),
                ('cli_turn_active_has_no_end',{'ended_at':datetime.now(timezone.utc)}),
                ('cli_turn_completed_requires_end',{'phase':'completed'})):
                await rejects(label,NativeCliTurn,dict(turn,id=str(uuid.uuid4()),client_message_id=label,**patch))
            await rejects('cli_turn_client_message_unique',NativeCliTurn,dict(turn,id=str(uuid.uuid4())))
            if not (await connection.execute(select(Conversation.id).where(Conversation.id==102))).scalar():
                await connection.execute(Conversation.__table__.insert().values(id=102,case_id=101,title='native fork migration'))
            await connection.execute(AgentEvent.__table__.insert().values(id=9001,conversation_id=101,seq=1,event_id='fork-boundary',type='message',payload='{}'))
            native_fork=dict(id=str(uuid.uuid4()),source_conversation_id=101,conversation_id=102,
                source_turn_id=turn['id'],boundary_event_id=9001,case_id=101,created_by=101,
                client_request_id='fork-migration',request_sha256='e'*64)
            # Every invalid candidate has a distinct destination, so a unique
            # constraint cannot mask the specific CHECK/FK being exercised.
            for index,(label,patch) in enumerate((
                ('source_fk',{'source_conversation_id':999}),('turn_fk',{'source_turn_id':'missing'}),
                ('event_fk',{'boundary_event_id':999}),('case_fk',{'case_id':999}),('user_fk',{'created_by':999}),
                ('invalid_phase',{'phase':'invented'}),('negative_version',{'version':-1}),
                ('empty_client',{'client_request_id':''}),('short_digest',{'request_sha256':'x'}),
                ('thread_requires_session',{'thread_id':'thread'}),('forking_requires_session',{'phase':'forking'}),
                ('completed_requires_thread',{'phase':'completed','session_id':cli['id'],'ended_at':datetime.now(timezone.utc)}),
                ('failed_requires_end',{'phase':'failed'}),('active_has_no_end',{'ended_at':datetime.now(timezone.utc)}))):
                destination=500+index
                if not (await connection.execute(select(Conversation.id).where(Conversation.id==destination))).scalar():
                    await connection.execute(Conversation.__table__.insert().values(id=destination,case_id=101,title='constraint fork'))
                await rejects('cli_fork_'+label,NativeCliFork,dict(native_fork,conversation_id=destination,**patch))
            await connection.execute(NativeCliFork.__table__.insert().values(**native_fork))
            await rejects('cli_fork_target_unique',NativeCliFork,dict(native_fork,id=str(uuid.uuid4()),client_request_id='different'))
            await rejects('cli_fork_request_unique',NativeCliFork,dict(native_fork,id=str(uuid.uuid4()),conversation_id=500))
            execution=dict(conversation_id=101,phase='active',backend='codex',resource_id=turn['id'],owner_id='migration-owner',
                owner_token_sha256='d'*64,owner_identity_json='{}',lease_expires_at=datetime.now(timezone.utc))
            await connection.execute(ConversationExecution.__table__.insert().values(**execution))
            for label,patch in (
                ('conversation_owner_fk',{'conversation_id':999}),('conversation_owner_phase',{'phase':'invented'}),
                ('conversation_owner_backend',{'backend':'invented'}),('conversation_owner_version',{'version':-1}),
                ('conversation_idle_has_no_owner',{'phase':'idle'}),('conversation_active_requires_owner',{'owner_id':None}),
                ('conversation_active_requires_lease',{'lease_expires_at':None})):
                if label=='conversation_owner_fk':values=dict(execution,**patch)
                else:
                    # Use another real conversation so its PK cannot mask the
                    # check constraint under test.
                    cid=2000+len(checked)
                    if not (await connection.execute(select(Conversation.id).where(Conversation.id==cid))).first():
                        await connection.execute(Conversation.__table__.insert().values(id=cid,case_id=101,title=label))
                    values=dict(execution,conversation_id=cid,**patch)
                await rejects(label,ConversationExecution,values)
            event=dict(turn_id=turn['id'],seq=1,type='turn.admitted',payload_json='{}')
            await connection.execute(NativeCliEvent.__table__.insert().values(**event))
            await rejects('cli_event_turn_fk',NativeCliEvent,dict(event,turn_id='missing'))
            await rejects('cli_event_positive_sequence',NativeCliEvent,dict(event,seq=0))
            await rejects('cli_event_sequence_unique',NativeCliEvent,event)
            approval=dict(id=str(uuid.uuid4()),turn_id=turn['id'],request_key='1:n:1',request_id_json='1',
                method='item/commandExecution/requestApproval',params_json='{}',request_sha256='e'*64)
            await connection.execute(NativeCliApproval.__table__.insert().values(**approval))
            for label,patch in (('cli_approval_turn_fk',{'turn_id':'missing'}),('cli_approval_phase',{'phase':'invented'}),
                                ('cli_approval_method',{'method':'invented'})):
                await rejects(label,NativeCliApproval,dict(approval,id=str(uuid.uuid4()),request_key=label,**patch))
            await rejects('cli_approval_request_unique',NativeCliApproval,dict(approval,id=str(uuid.uuid4())))
            at = datetime.now(timezone.utc)
            oauth = dict(id=str(uuid.uuid4()), state_sha256='1'*64, browser_sha256='2'*64, nonce_sha256='3'*64,
                config_sha256='4'*64, verifier_ciphertext='encrypted-fixture', intent='login', created_at=at,
                expires_at=at+timedelta(minutes=10))
            await connection.execute(WebsiteOAuthTransaction.__table__.insert().values(**oauth))
            await rejects('website_oauth_state_unique', WebsiteOAuthTransaction, dict(oauth, id=str(uuid.uuid4())))
            for label, patch in (
                ('website_oauth_phase', {'phase':'invented'}), ('website_oauth_link_owner_required', {'intent':'link'}),
                ('website_oauth_login_owner_absent', {'link_user_id':101}),
                ('website_oauth_link_owner_fk', {'intent':'link','link_user_id':999}),
                ('website_oauth_pending_verifier_required', {'verifier_ciphertext':None}),
                ('website_oauth_terminal_verifier_cleared', {'phase':'completed','ended_at':at}),
                ('website_oauth_terminal_end_required', {'phase':'failed','verifier_ciphertext':None}),
                ('website_oauth_expiry_order', {'expires_at':at})):
                await rejects(label, WebsiteOAuthTransaction, dict(oauth, id=str(uuid.uuid4()), state_sha256=uuid.uuid4().hex*2, **patch))
            external = dict(id=str(uuid.uuid4()), user_id=101, issuer='https://identity.example.test', client_id='fixture', subject='subject')
            await connection.execute(ExternalIdentity.__table__.insert().values(**external))
            await rejects('external_identity_subject_unique', ExternalIdentity, dict(external,id=str(uuid.uuid4())))
            await rejects('external_identity_user_fk', ExternalIdentity, dict(external,id=str(uuid.uuid4()),subject='other',user_id=999))
            login = dict(id=str(uuid.uuid4()),token_sha256='5'*64,user_id=101,identity_id=external['id'],created_at=at,expires_at=at+timedelta(hours=8))
            await connection.execute(WebsiteAuthSession.__table__.insert().values(**login))
            await rejects('website_auth_token_unique', WebsiteAuthSession,dict(login,id=str(uuid.uuid4())))
            for label,patch in (('website_auth_user_fk',{'user_id':999}),('website_auth_identity_fk',{'identity_id':'missing'}),
                                ('website_auth_expiry_order',{'expires_at':at})):
                await rejects(label,WebsiteAuthSession,dict(login,id=str(uuid.uuid4()),token_sha256=uuid.uuid4().hex*2,**patch))
    finally:
        await engine.dispose()
    return checked


async def run_database(url, report):
    report.mkdir(parents=True, exist_ok=False)
    from app.db import initialize_schema
    if url.startswith("postgresql"):
        bootstrap_engine = engine_for(url)
        try:
            async with bootstrap_engine.begin() as connection:
                try:
                    await initialize_schema(connection)
                    raise AssertionError("Empty PostgreSQL silently bootstrapped without migration")
                except RuntimeError as error:
                    assert "explicit Alembic upgrade" in str(error)
                assert not await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        finally:
            await bootstrap_engine.dispose()
    await asyncio.to_thread(alembic, url, report, "upgrade", BASE)
    before = await legacy(url, seed=True)
    guard_engine = engine_for(url)
    try:
        async with guard_engine.begin() as connection:
            try:
                await initialize_schema(connection)
                raise AssertionError("Startup silently upgraded the legacy database")
            except RuntimeError as error:
                assert "reviewed Alembic upgrade" in str(error)
            tables_before_upgrade = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
            assert len(tables_before_upgrade) == 13 and not NEW & tables_before_upgrade
    finally:
        await guard_engine.dispose()
    await asyncio.to_thread(alembic, url, report, "upgrade", "head")
    assert await legacy(url) == before
    checks = await schema_and_constraints(url)
    from app.models import OwnerStorageReservation
    owner_engine=engine_for(url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(OwnerStorageReservation.__table__.insert().values(id=str(uuid.uuid4()),user_id=101,
                binding_json='{}',writer_json='{}',size=100,remaining_bytes=100,state='active',receipt_json='{}'))
        for state in ('active','settled'):
            if state=='settled':
                async with owner_engine.begin() as connection:
                    await connection.execute(OwnerStorageReservation.__table__.update().values(state='settled',remaining_bytes=0,receipt_json='{"closed":true}'))
            try:await asyncio.to_thread(alembic,url,report,'downgrade','c0a5c71846ed')
            except AssertionError:pass
            else:raise AssertionError('Downgrade discarded owner capacity history')
            async with owner_engine.connect() as connection:
                assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()==HEAD
                assert (await connection.execute(select(OwnerStorageReservation.state))).scalar_one()==state
            checks.append('owner_capacity_'+state+'_downgrade_guard')
        async with owner_engine.begin() as connection:await connection.execute(delete(OwnerStorageReservation))
        await asyncio.to_thread(alembic,url,report,'downgrade','c0a5c71846ed')
    finally:await owner_engine.dispose()
    from app.models import StorageWriteReservation,CaseStorageOperation
    write_engine=engine_for(url)
    operation_id=str(uuid.uuid4())
    try:
        async with write_engine.begin() as connection:
            await connection.execute(CaseStorageOperation.__table__.insert().values(id=operation_id,case_id=101,
                source_prefix='workspaces/101/cases/101',purpose='staging',state='active',owner_json='{}'))
            await connection.execute(StorageWriteReservation.__table__.insert().values(id=str(uuid.uuid4()),
                operation_id=operation_id,user_id=101,case_id=101,binding_json='{}',request_json='{}',writer_json='{}',
                size=100,remaining_bytes=100,state='active',receipt_json='{}'))
        for state in ('active','settled'):
            if state=='settled':
                async with write_engine.begin() as connection:
                    await connection.execute(StorageWriteReservation.__table__.update().values(state='settled',remaining_bytes=0,receipt_json='{"status":200}'))
            try:await asyncio.to_thread(alembic,url,report,'downgrade','bf94b60735dc')
            except AssertionError:pass
            else:raise AssertionError('Downgrade discarded remote write quota history')
            async with write_engine.connect() as connection:
                assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='c0a5c71846ed'
                assert (await connection.execute(select(StorageWriteReservation.state))).scalar_one()==state
            checks.append('remote_write_'+state+'_downgrade_guard')
        async with write_engine.begin() as connection:
            await connection.execute(delete(StorageWriteReservation))
            await connection.execute(delete(CaseStorageOperation).where(CaseStorageOperation.id==operation_id))
        await asyncio.to_thread(alembic,url,report,'downgrade','bf94b60735dc')
    finally:await write_engine.dispose()
    from app.models import JobLogReservation,JobAttempt
    log_engine=engine_for(url)
    try:
        async with log_engine.begin() as connection:
            attempt_id=(await connection.execute(select(JobAttempt.id))).scalars().first()
            job_id=(await connection.execute(select(JobAttempt.job_id).where(JobAttempt.id==attempt_id))).scalar_one()
            await connection.execute(JobLogReservation.__table__.insert().values(
                job_id=job_id,attempt_id=attempt_id,user_id=101,case_id=101,binding_json='{}',
                sha256='a'*64,size=100,remaining_bytes=100,state='active',writer_json='{}',token=str(uuid.uuid4())))
        for state in ('active','materialized','released'):
            if state!='active':
                async with log_engine.begin() as connection:
                    await connection.execute(text('UPDATE job_log_reservations SET state=:state, remaining_bytes=0'),{'state':state})
            try:await asyncio.to_thread(alembic,url,report,'downgrade','ae83a5f924cb')
            except AssertionError:pass
            else:raise AssertionError('Downgrade discarded remote log capacity history')
            async with log_engine.connect() as connection:
                assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='bf94b60735dc'
                assert (await connection.execute(select(JobLogReservation.state))).scalar_one()==state
            checks.append('job_log_'+state+'_downgrade_guard')
        async with log_engine.begin() as connection:
            await connection.execute(delete(JobLogReservation))
        await asyncio.to_thread(alembic,url,report,'downgrade','ae83a5f924cb')
    finally:await log_engine.dispose()
    from app.models import OutputArchiveReservation,JobAttempt
    archive_engine=engine_for(url)
    try:
        async with archive_engine.begin() as connection:
            attempt_id=(await connection.execute(select(JobAttempt.id))).scalars().first()
            await connection.execute(OutputArchiveReservation.__table__.insert().values(
                attempt_id=attempt_id,user_id=101,case_id=101,binding_json='{}',input_sha256='b'*64,
                manifest_sha256='a'*64,size=100,remaining_bytes=100,state='active',writer_json='{}',token=str(uuid.uuid4())))
        for state in ('active','materialized','released'):
            if state!='active':
                async with archive_engine.begin() as connection:
                    await connection.execute(text('UPDATE output_archive_reservations SET state=:state, remaining_bytes=0'),{'state':state})
            try:await asyncio.to_thread(alembic,url,report,'downgrade','9d72f4e813ba')
            except AssertionError:pass
            else:raise AssertionError('Downgrade discarded output archive capacity history')
            async with archive_engine.connect() as connection:
                assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='ae83a5f924cb'
                assert (await connection.execute(select(OutputArchiveReservation.state))).scalar_one()==state
            checks.append('output_archive_'+state+'_downgrade_guard')
        async with archive_engine.begin() as connection:
            await connection.execute(delete(OutputArchiveReservation))
        await asyncio.to_thread(alembic,url,report,'downgrade','9d72f4e813ba')
    finally:await archive_engine.dispose()
    from app.models import PublicationStorageReservation,ArtifactPublication
    publication_engine=engine_for(url)
    try:
        async with publication_engine.begin() as connection:
            pub_id=(await connection.execute(select(ArtifactPublication.id))).scalars().first()
            await connection.execute(PublicationStorageReservation.__table__.insert().values(
                publication_id=pub_id,user_id=101,case_id=101,binding_json='{}',manifest_sha256='a'*64,
                size=100,remaining_bytes=100,state='active',writer_json='{}',token=str(uuid.uuid4())))
        for state in ('active','materialized','released'):
            if state!='active':
                async with publication_engine.begin() as connection:
                    await connection.execute(text('UPDATE publication_storage_reservations SET state=:state, remaining_bytes=0'),{'state':state})
            try:await asyncio.to_thread(alembic,url,report,'downgrade','8c61e3d702a9')
            except AssertionError:pass
            else:raise AssertionError('Downgrade discarded publication capacity history')
            async with publication_engine.connect() as connection:
                assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='9d72f4e813ba'
                assert (await connection.execute(select(PublicationStorageReservation.state))).scalar_one()==state
            checks.append('publication_capacity_'+state+'_downgrade_guard')
        async with publication_engine.begin() as connection:
            await connection.execute(delete(PublicationStorageReservation))
        await asyncio.to_thread(alembic,url,report,'downgrade','8c61e3d702a9')
    finally:await publication_engine.dispose()
    from app.models import UploadStorageReservation
    quota_engine=engine_for(url)
    claim_id=uuid.uuid4().hex
    try:
        async with quota_engine.begin() as connection:
            await connection.execute(UploadStorageReservation.__table__.insert().values(
                id=claim_id,user_id=101,case_id=101,binding_json='{}',path='Ref/preserved.bin',
                size=100,remaining_bytes=100,state='active'))
        for state in ('active','completed'):
            if state=='completed':
                async with quota_engine.begin() as connection:
                    await connection.execute(text("UPDATE upload_storage_reservations SET state='completed', remaining_bytes=0 WHERE id=:id"),{'id':claim_id})
            try:await asyncio.to_thread(alembic,url,report,'downgrade','7b50d2c6f198')
            except AssertionError:pass
            else:raise AssertionError('Downgrade discarded upload quota history')
            async with quota_engine.connect() as connection:
                assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='8c61e3d702a9'
                assert (await connection.execute(text('SELECT state FROM upload_storage_reservations WHERE id=:id'),{'id':claim_id})).scalar_one()==state
        async with quota_engine.begin() as connection:
            await connection.execute(delete(UploadStorageReservation).where(UploadStorageReservation.id==claim_id))
    finally:await quota_engine.dispose()
    # Exercise the immediate predecessor against populated request history.
    from app.models import (NativeRequest,NativeSession,NativeVizSession,NativeCliSession,ConversationExecution,NativeCliTurn,NativeCliEvent,NativeCliApproval,
        WebsiteOAuthTransaction,ExternalIdentity,WebsiteAuthSession,NativeCliFork,AgentEvent,NativeCliSessionRequest)
    request_engine=engine_for(url)
    try:
        # Reach this migration's predecessor before checking its loss guard;
        # SQLite does not roll back successful earlier DDL on a later failure.
        await asyncio.to_thread(alembic,url,report,'downgrade','4e2da9f3c865')
        async with request_engine.begin() as connection:
            await connection.execute(text("UPDATE job_attempts SET worker_identity_json=:record"),
                                     {'record':'{"retained_worker_evidence":true}'})
        try:await asyncio.to_thread(alembic,url,report,'downgrade','3d1c98e2b754')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded worker ownership history')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='4e2da9f3c865'
            assert (await connection.execute(text('SELECT worker_identity_json FROM job_attempts'))).scalar_one()=='{"retained_worker_evidence":true}'
            await connection.execute(text("UPDATE job_attempts SET worker_identity_json='{}'"))
            old_attempt = dict((await connection.execute(text('SELECT * FROM job_attempts'))).mappings().one())
        await asyncio.to_thread(alembic,url,report,'downgrade','3d1c98e2b754')
        await request_engine.dispose()  # Discard asyncpg SELECT * plans across external DDL.
        async with request_engine.connect() as connection:
            assert dict((await connection.execute(text('SELECT * FROM job_attempts'))).mappings().one())=={
                k:v for k,v in old_attempt.items() if k!='worker_identity_json'}
        await asyncio.to_thread(alembic,url,report,'upgrade','head')
        await request_engine.dispose()
        async with request_engine.connect() as connection:
            assert dict((await connection.execute(text('SELECT * FROM job_attempts'))).mappings().one())=={
                **old_attempt,'write_checkpoint_json':'{}'}
        checks.append('worker_identity_history_blocks_lossy_downgrade')
        # Reach the exact predecessor first; SQLite and PostgreSQL differ on
        # transactional rollback of a failed multi-revision downgrade.
        await asyncio.to_thread(alembic,url,report,'downgrade','3d1c98e2b754')
        try:await asyncio.to_thread(alembic,url,report,'downgrade','2c0b87d1a643')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded native startup history')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='3d1c98e2b754'
            assert (await connection.execute(select(NativeCliSessionRequest.id))).scalar_one()
            await connection.execute(delete(NativeCliSessionRequest))
        await asyncio.to_thread(alembic,url,report,'downgrade','2c0b87d1a643')
        checks.append('native_startup_history_blocks_lossy_downgrade')
        try:await asyncio.to_thread(alembic,url,report,'downgrade','1b9a76c0f532')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded native fork history')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='2c0b87d1a643'
            assert (await connection.execute(select(NativeCliFork.id))).scalar_one()
            await connection.execute(delete(NativeCliFork))
            await connection.execute(delete(AgentEvent).where(AgentEvent.id==9001))
        # Establish the same predecessor before exercising its guard: PostgreSQL
        # rolls back a multi-revision downgrade while SQLite may retain DDL.
        await asyncio.to_thread(alembic,url,report,'downgrade','1b9a76c0f532')
        async with request_engine.connect() as connection:
            before_native = dict((await connection.execute(select(NativeCliSession.__table__).where(NativeCliSession.runtime=='wsl'))).mappings().one())
        try:await asyncio.to_thread(alembic,url,report,'downgrade','0a8f65b9e421')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded WSL CLI history')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='1b9a76c0f532'
            assert dict((await connection.execute(select(NativeCliSession.__table__).where(NativeCliSession.runtime=='wsl'))).mappings().one())==before_native
            await connection.execute(delete(NativeCliSession).where(NativeCliSession.runtime=='wsl'))
            before_windows = dict((await connection.execute(select(NativeCliSession.__table__))).mappings().one())
            before_turn = dict((await connection.execute(select(NativeCliTurn.__table__))).mappings().one())
        await asyncio.to_thread(alembic,url,report,'downgrade','0a8f65b9e421')
        await asyncio.to_thread(alembic,url,report,'upgrade','head')
        async with request_engine.connect() as connection:
            assert dict((await connection.execute(select(NativeCliSession.__table__))).mappings().one())==before_windows
            assert dict((await connection.execute(select(NativeCliTurn.__table__))).mappings().one())==before_turn
        await asyncio.to_thread(alembic,url,report,'downgrade','0a8f65b9e421')
        for model in (WebsiteAuthSession, ExternalIdentity, WebsiteOAuthTransaction):
            try:await asyncio.to_thread(alembic,url,report,'downgrade','f97e54a8d310')
            except AssertionError:pass
            else:raise AssertionError('Downgrade silently discarded website identity history')
            async with request_engine.begin() as connection:
                assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='0a8f65b9e421'
                assert (await connection.execute(select(model))).first() is not None
                await connection.execute(delete(model))
        await asyncio.to_thread(alembic,url,report,'downgrade','f97e54a8d310')
        async with request_engine.connect() as connection:
            before_turn=dict((await connection.execute(select(NativeCliTurn.__table__))).mappings().one())
            before_owner=dict((await connection.execute(select(ConversationExecution.__table__))).mappings().one())
        try:await asyncio.to_thread(alembic,url,report,'downgrade','e86d43f7c209')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded native turn or conversation ownership')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='f97e54a8d310'
            assert dict((await connection.execute(select(NativeCliTurn.__table__))).mappings().one())==before_turn
            assert dict((await connection.execute(select(ConversationExecution.__table__))).mappings().one())==before_owner
            for model in (NativeCliApproval,NativeCliEvent,NativeCliTurn):await connection.execute(delete(model))
        # An active provider/Goal fence alone must still refuse downgrade.
        try:await asyncio.to_thread(alembic,url,report,'downgrade','e86d43f7c209')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded an active non-turn owner')
        async with request_engine.begin() as connection:await connection.execute(delete(ConversationExecution))
        await asyncio.to_thread(alembic,url,report,'downgrade','e86d43f7c209')
        async with request_engine.connect() as connection:
            before_cli=dict((await connection.execute(select(NativeCliSession.__table__))).mappings().one())
        try:await asyncio.to_thread(alembic,url,report,'downgrade','d75c32e6b1f8')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded CLI ownership history')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='e86d43f7c209'
            assert dict((await connection.execute(select(NativeCliSession.__table__))).mappings().one())==before_cli
            await connection.execute(delete(NativeCliSession).where(NativeCliSession.id==before_cli['id']))
        await asyncio.to_thread(alembic,url,report,'downgrade','d75c32e6b1f8')
        async with request_engine.connect() as connection:
            before_shell=dict((await connection.execute(select(NativeRequest.__table__).where(NativeRequest.kind=='agent.shell'))).mappings().one())
        try:await asyncio.to_thread(alembic,url,report,'downgrade','c64b21d5a0e7')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded Agent shell binding history')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='d75c32e6b1f8'
            assert dict((await connection.execute(select(NativeRequest.__table__).where(NativeRequest.id==before_shell['id']))).mappings().one())==before_shell
            await connection.execute(delete(NativeRequest).where(NativeRequest.id==before_shell['id']))
        await asyncio.to_thread(alembic,url,report,'downgrade','c64b21d5a0e7')
        # Older schemas predate only these two columns. Read the unchanged
        # legacy projection explicitly while exercising their history guards.
        legacy_request_columns=[column for column in NativeRequest.__table__.c if column.name not in {'conversation_id','tool_call_id'}]
        async with request_engine.connect() as connection:
            before_viz=dict((await connection.execute(select(NativeVizSession.__table__))).mappings().one())
        try:await asyncio.to_thread(alembic,url,report,'downgrade','b53a10c4f9d6')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded visualization history')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='c64b21d5a0e7'
            assert dict((await connection.execute(select(NativeVizSession.__table__))).mappings().one())==before_viz
            await connection.execute(delete(NativeVizSession).where(NativeVizSession.id==before_viz['id']))
        await asyncio.to_thread(alembic,url,report,'downgrade','b53a10c4f9d6')
        async with request_engine.connect() as connection:
            before_session=dict((await connection.execute(select(NativeSession.__table__))).mappings().one())
        try:
            await asyncio.to_thread(alembic,url,report,'downgrade','ad42f9b3e8c5')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded interactive session history')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='b53a10c4f9d6'
            assert dict((await connection.execute(select(NativeSession.__table__))).mappings().one())==before_session
            await connection.execute(delete(NativeSession).where(NativeSession.id==before_session['id']))
        # SQLite DDL is not assumed to roll back across revisions. First reach
        # the immediate global-request predecessor with its guard intact.
        await asyncio.to_thread(alembic,url,report,'downgrade','ad42f9b3e8c5')
        async with request_engine.begin() as connection:
            original=(await connection.execute(select(*legacy_request_columns))).mappings().one()
            before_request=dict(original)
            blocker_id=str(uuid.uuid4())
            await connection.execute(NativeRequest.__table__.insert().values(**dict(before_request,
                id=blocker_id,case_id=None,launch_nonce='f'*64,kind='runtime.wsl_probe')))
        try:
            await asyncio.to_thread(alembic,url,report,'downgrade','9c31e8a2d7b4')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently discarded global runtime ownership')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='ad42f9b3e8c5'
            assert (await connection.execute(select(NativeRequest.id).where(NativeRequest.id==blocker_id))).scalar_one()==blocker_id
            await connection.execute(delete(NativeRequest).where(NativeRequest.id==blocker_id))
        await asyncio.to_thread(alembic,url,report,'downgrade','9c31e8a2d7b4')
        async with request_engine.begin() as connection:
            assert dict((await connection.execute(select(*legacy_request_columns))).mappings().one())==before_request
            await connection.execute(NativeRequest.__table__.insert().values(**dict(before_request,
                id=blocker_id,launch_nonce='f'*64,kind='synctex.query')))
        try:
            await asyncio.to_thread(alembic,url,report,'downgrade','8b20d7f1c6a3')
        except AssertionError:pass
        else:raise AssertionError('Downgrade silently removed the SyncTeX history constraint')
        async with request_engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='9c31e8a2d7b4'
            await connection.execute(delete(NativeRequest).where(NativeRequest.id==blocker_id))
        await asyncio.to_thread(alembic,url,report,'downgrade','8b20d7f1c6a3')
        await asyncio.to_thread(alembic,url,report,'upgrade','head')
        async with request_engine.connect() as connection:
            assert dict((await connection.execute(select(*legacy_request_columns))).mappings().one())==before_request
    finally:
        await request_engine.dispose()
    # Existing attempts already have incoming head/event/publication FKs. Exercise
    # SQLite batch rebuild and PostgreSQL ALTER against those populated records.
    await asyncio.to_thread(alembic, url, report, "downgrade", "0fd328dae4cf")
    await asyncio.to_thread(alembic, url, report, "upgrade", "head")
    linked_engine = engine_for(url)
    try:
        async with linked_engine.connect() as connection:
            assert (await connection.execute(text("SELECT count(*) FROM job_attempts WHERE root_job_id=job_id"))).scalar_one() == 1
            assert (await connection.execute(text("SELECT child_processes_json FROM job_attempts"))).scalar_one() == '{}'
            assert (await connection.execute(text("SELECT count(*) FROM artifact_publications p JOIN job_attempts a ON p.attempt_id=a.id"))).scalar_one() == 1
            assert (await connection.execute(text("SELECT count(*) FROM durable_events e JOIN job_attempts a ON e.attempt_id=a.id"))).scalar_one() == 1
            assert (await connection.execute(text("SELECT count(*) FROM job_execution_heads h JOIN job_attempts a ON h.active_attempt_id=a.id"))).scalar_one() == 1
            if url.startswith("sqlite"):
                assert not (await connection.execute(text("PRAGMA foreign_key_check"))).all()
    finally:
        await linked_engine.dispose()
    await asyncio.to_thread(alembic, url, report, "check")
    await asyncio.to_thread(alembic, url, report, "upgrade", "head")
    await asyncio.to_thread(alembic, url, report, "downgrade", BASE)
    assert await legacy(url) == before, "Rollback changed legacy Job"
    engine = engine_for(url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
            assert not NEW & tables and len(tables) == 13
    finally:
        await engine.dispose()
    await asyncio.to_thread(alembic, url, report, "upgrade", "head")
    checks2 = await schema_and_constraints(url)
    assert checks == checks2 + [kind+'_'+state+'_downgrade_guard' for kind in ('owner_capacity','remote_write') for state in ('active','settled')] + [kind+'_'+state+'_downgrade_guard' for kind in ('job_log','output_archive','publication_capacity') for state in ('active','materialized','released')] + ['worker_identity_history_blocks_lossy_downgrade', 'native_startup_history_blocks_lossy_downgrade'] and await legacy(url) == before
    session_engine=engine_for(url)
    try:
        async with session_engine.begin() as connection:
            for model in (WebsiteAuthSession,ExternalIdentity,WebsiteOAuthTransaction):await connection.execute(delete(model))
            for model in (NativeCliSessionRequest,NativeCliFork,NativeCliApproval,NativeCliEvent,NativeCliTurn,ConversationExecution):await connection.execute(delete(model))
            await connection.execute(delete(AgentEvent).where(AgentEvent.id==9001))
            fixture_id=(await connection.execute(select(NativeSession.id))).scalar_one()
            await connection.execute(delete(NativeSession).where(NativeSession.id==fixture_id))
            viz_fixture_id=(await connection.execute(select(NativeVizSession.id))).scalar_one()
            await connection.execute(delete(NativeVizSession).where(NativeVizSession.id==viz_fixture_id))
            shell_fixture_id=(await connection.execute(select(NativeRequest.id).where(NativeRequest.kind=='agent.shell'))).scalar_one()
            await connection.execute(delete(NativeRequest).where(NativeRequest.id==shell_fixture_id))
            await connection.execute(delete(NativeCliSession))
    finally:await session_engine.dispose()
    await asyncio.to_thread(alembic, url, report, "downgrade", "base")
    await asyncio.to_thread(alembic, url, report, "upgrade", "head")
    await asyncio.to_thread(alembic, url, report, "check")
    result = {"passed": True, "tables":43, "constraint_checks":checks,
        "legacy_job_preserved":True, "startup_refuses_implicit_upgrade":True,
        "empty_postgres_requires_explicit_migration":url.startswith("postgresql"),
        "upgrade_rollback_reupgrade":True, "metadata_diff":0}
    result["populated_previous_revision_roundtrip"] = True
    result['populated_request_kind_roundtrip']=True
    result['downgrade_refuses_synctex_history']=True
    result['populated_global_scope_roundtrip']=True
    result['downgrade_refuses_global_runtime_history']=True
    result['downgrade_refuses_terminal_history']=True
    result['downgrade_refuses_viz_history']=True
    result['downgrade_refuses_agent_shell_history']=True
    result['downgrade_refuses_cli_history']=True
    result['downgrade_refuses_cli_turn_and_conversation_owners']=True
    result['downgrade_refuses_website_identity_history']=True
    result['downgrade_refuses_wsl_cli_history']=True
    result['downgrade_refuses_native_fork_history']=True
    result['downgrade_refuses_native_startup_history']=True
    result['downgrade_refuses_worker_ownership_history']=True
    result['populated_worker_identity_predecessor_roundtrip']=True
    result['populated_windows_cli_and_turn_roundtrip']=True
    result['populated_terminal_guard_preserves_all_fields']=True
    result['downgrade_refuses_active_and_completed_upload_quota_history']=True
    (report / "result.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres", action="store_true")
    args = parser.parse_args()
    root = SERVER.parents[1] / "tmp"
    root.mkdir(exist_ok=True)
    report = Path(tempfile.mkdtemp(prefix="workflow-migration-", dir=root))
    pg_url = None
    if args.postgres:
        from app.config import Settings
        pg_url = make_url(Settings(_env_file=SERVER / ".env").database_url)
        assert pg_url.drivername == "postgresql+asyncpg" and pg_url.host in {"127.0.0.1", "localhost", "::1"}, \
            "PostgreSQL migration test requires the configured local PostgreSQL server"
    sqlite_url = "sqlite+aiosqlite:///" + (report / "test.db").as_posix()
    # Any app.db import in this process is also bound to the disposable SQLite.
    os.environ["VIBEFLOW_DATABASE_URL"] = sqlite_url
    results = {"sqlite":await run_database(sqlite_url, report / "sqlite")}
    if pg_url:
        import asyncpg
        name = "tmp_vf_workflow_" + uuid.uuid4().hex
        assert re.fullmatch(r"tmp_vf_workflow_[0-9a-f]{32}", name)
        admin = await asyncpg.connect(host=pg_url.host, port=pg_url.port or 5432,
            user=pg_url.username, password=pg_url.password, database="postgres", timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "' + name + '"')
            created = True
            target = pg_url.set(database=name).render_as_string(hide_password=False)
            results["postgresql"] = await run_database(target, report / "postgresql")
        finally:
            if created:
                assert await admin.fetchval("SELECT current_database()") == "postgres"
                await admin.execute('DROP DATABASE "' + name + '"')
            await admin.close()
        results["postgresql"]["temporary_database_removed"] = True
    (report / "result.json").write_text(json.dumps(results, indent=2)+"\n", encoding="utf-8")
    print("WORKFLOW_MIGRATION_OK " + json.dumps({"databases":list(results), "evidence":str(report)}))


if __name__ == "__main__":
    asyncio.run(main())
