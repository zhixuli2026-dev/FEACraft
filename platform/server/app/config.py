"""VibeFlow Web Platform - 配置"""
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- 数据库：默认 sqlite（零依赖开发），生产/团队开发用 postgresql+asyncpg ---
    # 连接串统一由私有 VIBEFLOW_DATABASE_URL 提供，文档与源码不记录真实凭据。
    database_url: str = "sqlite+aiosqlite:///./vibeflow.db"
    # Deployment secret file; empty preserves the existing local app/.vault_key.
    vault_key_path: str = ""
    provider_kms_address: str = ""
    provider_kms_token_file: str = ""
    provider_kms_key_prefix: str = ""
    provider_kms_mount: str = "transit"
    provider_kms_ca_file: str = ""
    scnet_auth_origin: str = "https://api.scnet.cn"
    scnet_platform_origin: str = "https://www.scnet.cn"
    voice_transcriber_path: str = ""
    voice_model_path: str = ""
    offline_license_bundle_path: str = ""
    offline_license_public_key_path: str = ""
    offline_license_deployment: UUID | None = None
    offline_license_min_revocation_sequence: int = Field(default=1, ge=1)
    # Explicit runtime -> execution host mapping; empty preserves local delivery.
    job_execution_hosts: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Optional registered handler -> host overrides for mixed tool environments.
    job_handler_execution_hosts: dict[str, dict[str, str]] = Field(default_factory=dict)
    worker_host_queue: bool = False
    openfoam_source_library_dir: str = ""  # Absolute path on the solver execution host.

    # --- 对象存储 ---
    # backend=fs: 本地文件系统（storage_fs_root 下，当前默认——Docker 未就绪期的选择）
    # backend=s3: SeaweedFS（团队服务器形态）
    storage_backend: str = "fs"
    storage_fs_root: str = "./data/files"
    skills_storage_root: str = ""  # App-owned user/org skills; default server/data/skills.
    storage_measurement_timeout_seconds: float = Field(default=30, ge=.1, le=300)
    storage_measurement_max_entries: int = Field(default=1000000, ge=1, le=10000000)
    storage_measurement_concurrency: int = Field(default=4, ge=1, le=32)
    storage_measurement_page_size: int = Field(default=1000, ge=1, le=1000)
    run_derive_timeout_seconds: float = Field(default=300, ge=.1, le=3600)
    case_copy_timeout_seconds: float = Field(default=300, ge=.1, le=3600)
    viz_prepare_timeout_seconds: float = Field(default=300, ge=.1, le=3600)
    viz_session_lifetime_seconds: float = Field(default=0, ge=0, le=30*86400)
    viz_session_max_output_bytes: int = Field(default=0, ge=0, le=2**63-1)
    viz_session_workspace_limit_mb: int = Field(default=0, ge=0)
    viz_owner_routes: dict[str, str] = Field(default_factory=dict)  # API hostname -> private API origin.
    terminal_owner_routes: dict[str, str] = Field(default_factory=dict)
    terminal_prepare_timeout_seconds: float = Field(default=300, ge=.1, le=3600)
    terminal_session_lifetime_seconds: float = Field(default=0, ge=0, le=30*86400)
    terminal_session_max_output_bytes: int = Field(default=0, ge=0, le=2**63-1)
    terminal_session_workspace_limit_mb: int = Field(default=0, ge=0)
    codex_concurrent_sessions: int = Field(default=4, ge=1, le=128)
    codex_session_lifetime_seconds: float = Field(default=0, ge=0, le=30*86400)
    codex_session_max_output_bytes: int = Field(default=0, ge=0, le=2**63-1)
    node_storage_limit_mb: int = Field(default=10240, ge=1)
    node_storage_min_free_mb: int = Field(default=256, ge=0)
    execution_workspace_limit_mb: int = Field(default=4096, ge=1)
    execution_storage_poll_seconds: float = Field(default=.25, ge=.05, le=5)
    execution_windows_cpu_limit_seconds: int = Field(default=0, ge=0, le=86400)
    execution_windows_memory_limit_mb: int = Field(default=0, ge=0, le=1024*1024)
    execution_wsl_cpu_limit_seconds: int = Field(default=0, ge=0, le=86400)
    execution_wsl_memory_limit_mb: int = Field(default=0, ge=0, le=1024*1024)
    execution_linux_cpu_limit_seconds: int = Field(default=0, ge=0, le=86400)
    execution_linux_memory_limit_mb: int = Field(default=0, ge=0, le=1024*1024)
    storage_maintenance_enabled: bool = True
    file_watch_native: bool = True
    storage_maintenance_interval_seconds: int = Field(default=300, ge=1, le=86400)
    provision_stage_retention_seconds: int = Field(default=86400, ge=60)
    provision_history_retention_days: int = Field(default=0, ge=0)  # 0 retains receipts indefinitely; SQL proof and claims always remain.

    # --- S3（SeaweedFS，backend=s3 时使用）---
    s3_endpoint: str = "http://localhost:8333"
    s3_access_key: str = "any"          # SeaweedFS 默认不校验；生产用 admin 配置
    s3_secret_key: str = "any"
    s3_bucket: str = "vibeflow"
    s3_region: str = "us-east-1"
    allow_partial_s3: bool = False  # 仅限文件 API 测试；完整 CAE 链未接通前不得默认开启

    # --- 认证 ---
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_ttl_hours: int = 72
    siwc_client_id: str = ''
    siwc_client_secret: str = ''
    siwc_token_auth_method: str = 'none'  # Registered method: none or client_secret_basic.
    siwc_public_origin: str = ''  # HTTPS origin; callback path is fixed by the server.
    oidc_public_origin: str = ''
    oidc_providers: dict[str, dict] = Field(default_factory=dict)
    oidc_provider_file: str = ''
    oidc_issuer: str = ''
    oidc_client_id: str = ''
    oidc_client_secret: str = ''
    oidc_redirect_uri: str = ''
    semantic_scholar_api_key: SecretStr | None = None

    # --- 服务 ---
    host: str = "127.0.0.1"
    port: int = 8000
    job_lease_seconds: int = Field(default=30, ge=10, le=3600)
    job_heartbeat_seconds: int = Field(default=10, ge=1, le=600)
    cors_origins: str = "http://localhost:5173"
    monitoring_export_token: str = ''
    monitoring_db_path: str = ''
    codex_state_root: str = ""  # Private local state, outside public case storage.
    codex_windows_sandbox: Literal["elevated", "unelevated"] | None = None
    codex_wsl_path: str = ""  # Operator-selected Windows path to a Linux x86_64 ELF, never a web-supplied command.
    codex_wsl_state_root: str = ""  # Native Linux filesystem directory, provisioned for the non-root worker.
    acp_docker_image: str = ""  # Local, operator-selected image containing Linux ACP harnesses.
    acp_docker_deployment: str = ""  # Stable UUID for this deployment's Docker state volumes.

    @model_validator(mode='after')
    def validate_acp_docker(self):
        if bool(self.acp_docker_image) != bool(self.acp_docker_deployment):
            raise ValueError('Docker ACP requires an image and a stable deployment UUID')
        if self.acp_docker_deployment:
            from uuid import UUID
            if self.acp_docker_image.startswith('-') or any(c.isspace() for c in self.acp_docker_image):
                raise ValueError('Invalid Docker ACP image name')
            UUID(self.acp_docker_deployment)
        return self

    @model_validator(mode='after')
    def validate_offline_license(self):
        selected = (self.offline_license_bundle_path, self.offline_license_public_key_path, self.offline_license_deployment)
        if any(selected) and not all(selected):
            raise ValueError('Offline license requires a bundle, an issuer public key and a deployment identifier')
        return self

    @model_validator(mode='after')
    def validate_job_lease(self):
        if self.job_heartbeat_seconds * 2 >= self.job_lease_seconds:
            raise ValueError('Job heartbeat interval must be less than half the lease duration')
        return self

    @model_validator(mode='after')
    def validate_session_owner_routes(self):
        for hostname, origin in (*self.viz_owner_routes.items(), *self.terminal_owner_routes.items()):
            uri = urlsplit(origin)
            if (not hostname or ':' in hostname or not hostname.isascii() or len(hostname)>96
                    or uri.scheme not in ('http', 'https') or not uri.hostname or not uri.port
                    or uri.username is not None or uri.path not in ('', '/') or uri.query or uri.fragment):
                raise ValueError('Session owner routes require an API hostname and a private HTTP(S) origin with port')
        return self

    class Config:
        env_prefix = "VIBEFLOW_"
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_storage_backend(settings: Settings):
    if settings.storage_backend == "s3" and not settings.allow_partial_s3:
        raise RuntimeError(
            "S3 尚未覆盖作业/索引/终端/几何/viz 的完整执行链。"
            "请改用 fs；仅做受限文件 API 测试时可显式设置 "
            "VIBEFLOW_ALLOW_PARTIAL_S3=true"
        )
