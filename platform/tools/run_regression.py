"""默认离线回归；core/cae 使用隔离服务，all 为显式选择的遗留开发环境全集。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime
import os
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
OFFLINE = {"native_channel_smoke", "cli_discovery_smoke", "control_identity_smoke", "codex_wsl_preparation_smoke", "codex_session_request_smoke", "codex_model_options_smoke", "codex_usage_smoke", "codex_image_smoke", "codex_fork_smoke", "codex_conversation_smoke", "codex_wsl_state_smoke", "codex_wsl_transport_smoke", "codex_wsl_session_smoke",
           "terminal_case_activity_smoke", "upload_coordination_smoke", "case_validation_smoke", "validation_native_smoke", "validation_job_smoke", "validation_access_smoke", "job_drain_recovery_smoke", "job_launch_registration_smoke", "child_launch_registration_smoke", "process_preparation_smoke", "session_startup_recovery_smoke", "job_reconciliation_notice_smoke", "job_delivery_recovery_smoke", "legacy_job_admission_smoke", "event_consumer_smoke",
           "publication_preparation_recovery_smoke", "publication_finalizing_smoke", "publication_alias_journal_smoke", "publication_alias_recovery_smoke", "publication_journal_smoke", "publication_journal_api_smoke", "job_query_compat_smoke", "website_auth_smoke", "codex_credentials_smoke", "codex_turn_api_smoke", "codex_turn_staging_smoke", "codex_turn_owner_smoke",
           "codex_session_smoke", "codex_session_staging_smoke",
           "codex_stdio_smoke", "codex_turn_smoke", "codex_account_smoke", "codex_sandbox_smoke",
           "codex_capacity_smoke", "codex_history_smoke", "codex_owned_stdio_smoke",
           "animation_smoke", "colormap_smoke", "logparse_smoke", "plan_smoke",
           "casedict_schema_unit_smoke", "core_integrity_smoke", "s3_staging_smoke",
           "report_snapshot_smoke", "compose_smoke", "validate_audit",
           "source_baseline_smoke", "fault_fixture_smoke", "job_finalization_smoke", "job_finalizing_io_smoke", "publication_copy_smoke", "publication_verify_smoke", "publication_source_smoke", "job_release_smoke", "native_release_smoke", "job_owner_startup_smoke", "job_launch_lease_smoke",
           "workflow_migration_smoke", "workflow_state_smoke", "durable_outbox_api_smoke", "event_replay_smoke",
           "job_claim_smoke", "job_idempotency_api_smoke", "workflow_scheduler_smoke", "case_write_smoke", "backup_manifest_smoke",
           "trash_control_smoke", "professional_control_smoke", "directory_publish_smoke", "attempt_workspace_smoke", "default_workspace_smoke", "pdf_publication_smoke", "ppt_publication_smoke", "legacy_postprocess_publication_smoke", "calculix_publication_smoke", "early_finalization_smoke",
           'geometry_publication_smoke', 'publication_input_smoke', 'workflow_handler_smoke', 'mesh_handler_smoke', 'solve_handler_smoke', 'postprocess_handler_smoke', 'report_handler_smoke', 'compose_handler_smoke', 'latex_handler_smoke', 'ordinary_document_smoke',
           'process_identity_smoke','owned_process_smoke','process_registration_smoke','owned_tree_smoke','owned_tree_races_smoke','process_tree_smoke','process_lifetime_smoke','mpi_scope_smoke','publication_preflight_smoke','native_execution_smoke','pandoc_execution_smoke','job_terminal_smoke','native_request_smoke','document_admission_smoke','synctex_execution_smoke','runtime_probe_smoke','terminal_execution_smoke','terminal_staging_smoke','viz_execution_smoke','viz_staging_smoke','viz_concurrency_smoke','agent_shell_smoke','agent_shell_staging_smoke'}
CAE = {"golden_cavity", "golden_cavity3d", "thermal_conduction_smoke"}
OFFLINE.add('su2_publication_smoke')
OFFLINE.add('registered_delivery_race_smoke')
OFFLINE.add('job_budget_lifetime_smoke')
OFFLINE.add('native_budget_smoke')
OFFLINE.add('cumulative_output_budget_smoke')
OFFLINE.add('native_diagnostics_smoke')
OFFLINE.add('recovery_budget_smoke')
OFFLINE.add('storage_usage_smoke')
OFFLINE.add('storage_admission_smoke')
OFFLINE.add('trash_storage_usage_smoke')
OFFLINE.add('seal_storage_budget_smoke')
OFFLINE.add('node_storage_budget_smoke')
OFFLINE.add('execution_storage_smoke')
OFFLINE.add('recovery_storage_smoke')
OFFLINE.add('publication_storage_smoke')
OFFLINE.add('output_archive_storage_smoke')
OFFLINE.add('job_log_storage_smoke')
OFFLINE.add('staging_download_storage_smoke')
OFFLINE.add('remote_write_budget_smoke')
OFFLINE.add('local_provision_storage_smoke')
OFFLINE.add('owner_storage_budget_smoke')
OFFLINE.add('case_maintenance_storage_smoke')
OFFLINE.add('file_owner_storage_smoke')
OFFLINE.add('local_file_storage_smoke')
OFFLINE.add('read_owner_storage_smoke')
OFFLINE.add('provision_node_storage_smoke')
OFFLINE.add('publication_node_storage_smoke')
OFFLINE.update({'case_provision_smoke','s3_case_provision_smoke','case_provision_edges_smoke'})
OFFLINE.add('case_constructor_recovery_smoke')
OFFLINE.add('storage_maintenance_smoke')
OFFLINE.add('attempt_seal_recovery_smoke')
OFFLINE.add('private_cache_recovery_smoke')
OFFLINE.add('attempt_node_cache_smoke')
OFFLINE.add('attempt_plan_write_smoke')
OFFLINE.add('case_provision_stage_smoke')
OFFLINE.add('staging_sync_smoke')
OFFLINE.add('staging_transfer_smoke')
OFFLINE.add('interactive_staging_liveness_smoke')
OFFLINE.add('storage_operation_migration_smoke')
OFFLINE.add('alias_invalidation_smoke')
OFFLINE.add('report_history_smoke')
OFFLINE.update({'latex_history_smoke','latex_history_native_smoke'})
OFFLINE.update({'report_derivative_history_smoke','report_derivative_native_smoke'})
OFFLINE.add('geometry_parameter_binding_smoke')
OFFLINE.add('s3_live_store_smoke')
OFFLINE.add('paraview_publication_smoke')
OFFLINE.add('mpi_publication_smoke')
OFFLINE.update({'udf_publication_smoke','udf_admission_smoke'})
OFFLINE.add('flotherm_publication_smoke')
OFFLINE.add('flotherm_library_publication_smoke')
OFFLINE.add('case_command_publication_smoke')
OFFLINE.add('case_command_solve_smoke')
OFFLINE.add('s3_attempt_smoke')
OFFLINE.add('s3_publication_objects_smoke')
OFFLINE.update({'s3_alias_objects_smoke','s3_typed_publication_smoke'})
OFFLINE.update({'case_read_view_smoke','s3_core_workflow_smoke','s3_markdown_report_smoke'})
OFFLINE.add('s3_listing_smoke')
OFFLINE.add('unsealed_delivery_smoke')
OFFLINE.update({'dependency_graph_smoke', 'dependency_status_smoke', 'dependency_workflow_smoke'})
OFFLINE.update({'workflow_recompute_plan_smoke', 'workflow_recompute_smoke'})
OFFLINE.add('workflow_recompute_control_smoke')
OFFLINE.add('file_read_consistency_smoke')
OFFLINE.add('file_transactions_smoke')
OFFLINE.add('generated_file_transactions_smoke')
OFFLINE.add('visible_file_transactions_smoke')
OFFLINE.add('casedict_transactions_smoke')
OFFLINE.add('s3_file_transactions_smoke')
OFFLINE.add('memory_transactions_smoke')
OFFLINE.add('upload_transactions_smoke')
OFFLINE.update({'s3_upload_transactions_smoke', 's3_streams_smoke'})
OFFLINE.update({'s3_document_workflows_smoke','s3_document_conflicts_smoke'})
OFFLINE.add('s3_document_engineering_smoke')
OFFLINE.add('s3_professional_handlers_smoke')
CORE = ("core_api_smoke", "stage_advance_smoke", "workflow_fallback_smoke",
        "smoke_test", "casedict_schema_smoke", "casedict_sync_smoke", "case_events_smoke",
        "provenance_smoke", "mesh_quality_smoke", "postprocess_smoke", "report_compose_smoke",
        "report_lifecycle_smoke", "latex_smoke", "latex_recipes_smoke")
EXECUTION = ("batch_jobs_smoke", "job_summary_smoke", "job_history_smoke", "job_index_smoke")
WRITES = ("events_smoke", "extensions_smoke", "error_lifecycle_smoke", "proposal_smoke", "upload_advanced_smoke",
          "case_admin_smoke", "memory_smoke", "plan_mode_smoke", "image_attachment_smoke", "backup_smoke",
          "case_import_smoke", "trash_smoke", "geometry_repair_smoke", "udf_workflow_smoke", "cadscene_smoke", "mpi_smoke",
          "ppt_smoke", "pdf_number_smoke", "calculix_smoke", "su2_smoke", "paraview_smoke", "flotherm_library_smoke", "flotherm_api_smoke", "terminal_smoke", "terminal_executor_smoke", "viz_smoke", "viz_idle_smoke",
          'geometry_smoke', 'geo_context_smoke', 'geometry_validation_smoke', 'step_preview_smoke')


def selected(only: str | None, exclude: str | None, profile: str = "all") -> list[Path]:
    scripts = sorted(SERVER.glob("*_smoke.py"))
    scripts += [SERVER / name for name in ("smoke_test.py", "golden_cavity.py", "golden_cavity3d.py")]
    scripts += [ROOT / "infra" / "compose_smoke.py", ROOT / "tools" / "validate_audit.py"]
    if profile == "offline":
        scripts = [path for path in scripts if path.stem in OFFLINE]
    elif profile == "cae":
        scripts = [path for path in scripts if path.stem in CAE]
    elif profile == "core":
        scripts = [SERVER / f"{name}.py" for name in CORE]
    elif profile == "execution":
        scripts = [SERVER / f"{name}.py" for name in EXECUTION]
    elif profile == "writes":
        scripts = [SERVER / f"{name}.py" for name in WRITES]
    if only:
        names = {name.strip().removesuffix(".py") for name in only.split(",") if name.strip()}
        scripts = [path for path in scripts if path.stem in names]
        found = {path.stem for path in scripts}
        if found != names:
            raise SystemExit(f"未知套件: {sorted(names - found)}")
    if exclude:
        names = {name.strip().removesuffix(".py") for name in exclude.split(",") if name.strip()}
        scripts = [path for path in scripts if path.stem not in names]
    return scripts


def ci_scripts(stage):
    integrity = [SERVER / "core_api_smoke.py"]
    frontend = [path for path in sorted((ROOT / "tools").glob("*-smoke.mjs")) if path.name != "pty-smoke.mjs"]
    if stage == "integrity":
        return integrity
    if stage == "frontend":
        return frontend
    return [*dict.fromkeys([*selected(None, None, "offline"), *integrity, *frontend])]


def run_ci(stage):
    from regression_llm_policy import enforce

    scripts = ci_scripts(stage)
    enforce(scripts)
    if stage == "preflight":
        print(f"CI_LLM_REVIEW_OK {len(scripts)}")
        return

    command, directory = ([sys.executable, "-B"], SERVER) if stage == "integrity" else (["node"], ROOT / "web")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for path in scripts:
        print(f"CI_REGRESSION_START {path.relative_to(ROOT).as_posix()}", flush=True)
        result = subprocess.run([*command, str(path)], cwd=directory, env=env)
        if result.returncode:
            raise SystemExit(result.returncode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="逗号分隔套件名，例如 monitor_smoke,validate_audit")
    parser.add_argument("--exclude", help="逗号分隔排除套件名")
    parser.add_argument("--timeout", type=int, help="单套超时秒数；默认600，原生LaTeX注册套件3600")
    parser.add_argument("--logs", type=Path)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--profile", choices=("all", "offline", "core", "cae", "execution", "writes"), default="offline")
    parser.add_argument("--ci-stage", choices=("preflight", "integrity", "frontend"))
    args = parser.parse_args()
    if args.ci_stage:
        return run_ci(args.ci_stage)
    scripts = selected(args.only, args.exclude, args.profile)
    if args.list:
        for path in scripts:
            print(path.relative_to(ROOT))
        return
    # Check the whole selection before starting servers, creating users or
    # launching any test. Unknown and historical simulated suites fail closed.
    from regression_llm_policy import enforce
    enforce(scripts)
    logs = args.logs or ROOT.parent / "tmp" / (
        "vibeflow-regression-" + args.profile + "-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        + "-" + uuid.uuid4().hex[:6])
    logs.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    failed = []
    outcomes = []
    print(f"REGRESSION_START {len(scripts)} LOGS={logs}", flush=True)
    with ExitStack() as stack:
        if args.profile in {"core", "execution", "writes"}:
            sys.path.insert(0, str(SERVER))
            from testsupport import IsolatedServer
            api = stack.enter_context(IsolatedServer("core-regression"))
            api.call("POST", "/auth/register", {"email": "dev@vibeflow.cn",
                     "password": "devpass123", "name": "Disposable regression user"})
            env = dict(api.env, VIBEFLOW_TEST_BASE=api.base + "/api/v1")
        for index, path in enumerate(scripts, 1):
            output = logs / f"{path.stem}.log"
            started = time.monotonic()
            timed_out = False
            timeout = args.timeout if args.timeout is not None else (3600 if path.stem == 'latex_handler_smoke' else 600)
            try:
                with output.open("w", encoding="utf-8") as handle:
                    result = subprocess.run([sys.executable, "-B", str(path)], cwd=SERVER,
                                            stdout=handle, stderr=subprocess.STDOUT,
                                            env=env, timeout=timeout)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = -1
                timed_out = True
            outcomes.append({'suite':path.stem, 'exit_code':code, 'timed_out':timed_out,
                'timeout_seconds':timeout, 'elapsed_seconds':round(time.monotonic()-started,3)})
            print(f"{index}/{len(scripts)} {'PASS' if code == 0 else 'FAIL'} {path.stem}", flush=True)
            if code:
                failed.append(path.stem)
                print(f"REGRESSION_FAILURE {json.dumps(outcomes[-1])}", flush=True)
                if timed_out:
                    print('Test driver timed out; verify its isolated API/native cleanup before rerunning.', flush=True)
    (logs / "summary.json").write_text(json.dumps({"profile": args.profile,
        "total": len(scripts), "passed": len(scripts) - len(failed), "failed": failed,
        "suites": [path.stem for path in scripts], "outcomes":outcomes}, indent=2), encoding="utf-8")
    print(f"REGRESSION_RESULT {len(scripts) - len(failed)}/{len(scripts)} FAILED={failed} LOGS={logs}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
