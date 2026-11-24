"""离线验证生产 compose：缺密钥必须失败，且不得包含开发默认密钥。"""
import os
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
COMPOSE = ROOT / "docker-compose.prod.yml"
REPLICAS = ROOT / "docker-compose.replicas.yml"
NGINX_UPSTREAM_DIRECTORY = "/etc/nginx/vibeflow-upstreams"


def compose_config(env, *, distributed=False, replicas=False):
    # Config rendering needs neither a daemon nor the user's registry credentials.
    with tempfile.TemporaryDirectory(prefix="vf-compose-config-") as directory:
        (Path(directory) / "config.json").write_text("{}", encoding="utf-8")
        empty_env = Path(directory) / "empty.env"
        empty_env.write_text("", encoding="utf-8")
        command = ["docker", "compose", "--env-file", str(empty_env), "-f", str(COMPOSE)]
        if replicas:
            command += ["-f", str(REPLICAS)]
        if distributed:
            command += ["--profile", "distributed"]
        return subprocess.run(
            command + ["config", "--format", "json"],
            cwd=ROOT, env={**env, "DOCKER_CONFIG": directory}, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )


def main():
    missing = os.environ.copy()
    missing.pop("PG_PASSWORD", None)
    missing.pop("JWT_SECRET", None)
    result = compose_config(missing)
    assert result.returncode != 0, "缺少 PG_PASSWORD/JWT_SECRET 时 compose config 应失败"
    print("1. 缺少生产密钥时配置拒绝 [OK]")

    configured = missing.copy()
    configured["PG_PASSWORD"] = "test-only-postgres-password"
    configured["JWT_SECRET"] = "test-only-jwt-secret"
    result = compose_config(configured)
    assert result.returncode == 0, result.stderr
    rendered = result.stdout + result.stderr
    assert "vibeflow_dev_pwd" not in rendered
    assert "change-me-in-prod" not in rendered
    assert "test-only-postgres-password" in rendered
    assert "test-only-jwt-secret" in rendered
    print("2. 显式测试密钥可渲染且无开发默认值 [OK]")

    dockerfile = (ROOT / "Dockerfile.web").read_text(encoding="utf-8")
    assert "RUN npm run build" in dockerfile
    assert "RUN npx vite build" not in dockerfile
    print("3. 前端镜像执行完整类型检查与构建 [OK]")

    ignore_file = ROOT.parent / ".dockerignore"
    assert ignore_file.is_file(), "缺少 platform/.dockerignore"
    ignore = ignore_file.read_text(encoding="utf-8")
    required = {"**/.env", "**/.vault_key", "server/.venv", "server/data",
                "web/node_modules", "web/dist", "infra/wsl-vibeflow-linux"}
    missing_patterns = sorted(p for p in required if p not in ignore)
    assert not missing_patterns, missing_patterns
    print("4. 构建上下文排除秘密、依赖和大型本地产物 [OK]")

    api_dockerfile = (ROOT / "Dockerfile.api").read_text(encoding="utf-8")
    assert "COPY server/data/completion ./data/completion" in api_dockerfile
    assert "!server/data/completion" in ignore
    print("5. API 镜像包含 OpenFOAM 补全资产且不携带其他用户数据 [OK]")

    compose_text = COMPOSE.read_text(encoding="utf-8")
    assert "VIBEFLOW_STORAGE_BACKEND: ${VIBEFLOW_STORAGE_BACKEND:-fs}" in compose_text
    assert "VIBEFLOW_STORAGE_FS_ROOT: /data/files" in compose_text
    assert "casedata:/data/files" in compose_text
    assert "VIBEFLOW_ALLOW_PARTIAL_S3: ${VIBEFLOW_ALLOW_PARTIAL_S3:-false}" in compose_text
    assert 'profiles: ["s3"]' in compose_text
    print("6. 生产默认持久化 fs，S3 仅显式受限启用 [OK]")

    assert '"--no-access-log"' in api_dockerfile
    nginx = (ROOT / "nginx-web.conf").read_text(encoding="utf-8")
    assert "access_log /var/log/nginx/access.log vibeflow_noquery;" in nginx
    log_format = nginx.split("log_format vibeflow_noquery", 1)[1].split(";", 1)[0]
    assert "$request " not in log_format and "$query_string" not in log_format
    assert "$request_method $uri" in log_format
    assert "Content-Security-Policy" in nginx and "frame-ancestors 'self'" in nginx
    assert "object-src 'none'" in nginx and "worker-src 'self' blob:" in nginx
    assert "VIBEFLOW_RATE_LIMIT_API_PER_MINUTE" in compose_text
    assert "VIBEFLOW_RATE_LIMIT_LLM_PER_MINUTE" in compose_text
    print("7. J18 前端 CSP、访问日志脱敏与 J19 限流配置 [OK]")
    assert 'profiles: ["distributed"]' in compose_text
    assert 'command: ["python", "-m", "app.services.distributed_worker"]' in compose_text
    assert "VIBEFLOW_REDIS_PASSWORD" in compose_text and "redisdata:/data" in compose_text
    print("8. J14 显式 distributed Redis/arq worker profile [OK]")
    configured.update(REDIS_PASSWORD="test-only-redis-$-quote-'", VIBEFLOW_DISTRIBUTED_JOBS="true")
    result = compose_config(configured, distributed=True)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    redis = services["redis"]
    # Compose escapes literal dollars in its reusable config output.
    rendered_password = configured["REDIS_PASSWORD"].replace("$", "$$")
    assert redis["environment"]["REDIS_PASSWORD"] == rendered_password
    assert redis["command"] == ["redis-server", "--requirepass", rendered_password,
                                "--port", "6380", "--appendonly", "yes"]
    assert "redis-cli -p 6380" in redis["healthcheck"]["test"][1]
    for name in ("api", "arq-worker"):
        assert services[name]["environment"]["VIBEFLOW_DISTRIBUTED_JOBS"] == "true"
        assert services[name]["environment"]["VIBEFLOW_REDIS_PASSWORD"] == rendered_password
        assert services[name]["environment"]["VIBEFLOW_VAULT_KEY_PATH"] == "/run/secrets/provider_vault"
        assert any(secret["source"] == "provider_vault" for secret in services[name]["secrets"])
        assert services[name]["environment"]["VIBEFLOW_RUNTIME_CONFIG"] == "/data/runtime/runtime-config.json"
        assert any(v['source'] == 'runtimeconfig' and v['target'] == '/data/runtime'
                   for v in services[name]['volumes'])
        assert services[name]['environment']['VIBEFLOW_INDEX_ROOT'] == '/data/index'
        assert any(v['source'] == 'indexdata' and v['target'] == '/data/index'
                   for v in services[name]['volumes'])
    assert services["arq-worker"]["command"] == ["python", "-m", "app.services.distributed_worker"]
    assert services["arq-worker"]["stop_grace_period"] == "2m0s"
    assert "mock_llm.py" not in api_dockerfile
    assert "COPY server/alembic.ini ./alembic.ini" in api_dockerfile
    assert "COPY server/migrations ./migrations" in api_dockerfile
    print("9. 分布式配置实际渲染：认证/端口/AOF/新 worker/迁移资产 [OK]")
    assert "provider_vault" in json.loads(result.stdout)["secrets"]
    assert "infra/secrets" in ignore and "**/*.key" in ignore
    print("10. API/worker 共用部署密钥，构建上下文排除密钥文件 [OK]")
    print("11. API/worker 共用持久化工具路径配置卷 [OK]")
    print("12. API/worker 共用同主机持久化索引卷 [OK]")
    for name in ('api', 'arq-worker'):
        assert services[name]['security_opt'] == ['seccomp=unconfined']
    for name in ('postgres', 'redis', 'web'):
        assert not services[name].get('security_opt')
    print("13. Linux 作业命名空间配置仅应用于 API/worker [OK]")
    assert 'client_max_body_size 101m;' in nginx
    assert 'proxy_connect_timeout 3s;' in nginx
    api_location = nginx.split('location /api/ {', 1)[1].split('}', 1)[0]
    assert 'proxy_request_buffering off;' in api_location
    assert 'proxy_http_version 1.1;' in api_location
    print("14. 反代允许现有备份上限且正文直接进入 API 限额/期限 [OK]")

    def assert_upstream_mount(web, expected):
        mounted = [volume for volume in web["volumes"] if volume["target"] == NGINX_UPSTREAM_DIRECTORY]
        assert len(mounted) == 1
        assert mounted[0]["type"] == "bind" and mounted[0]["read_only"] is True
        assert Path(mounted[0]["source"]).resolve() == expected.resolve()

    default = ROOT / "nginx-upstream-default"
    replica_defaults = ROOT / "nginx-upstream-replicas"
    assert_upstream_mount(services["web"], default)
    assert f"include {NGINX_UPSTREAM_DIRECTORY}/api.conf;" in nginx
    assert (default / "api.conf").read_text().splitlines() == ["server api:8000 resolve;"]
    assert (replica_defaults / "api.conf").read_text().splitlines() == [
        "server api-primary:8000 resolve;", "server api-peer:8000 resolve;",
    ]
    web_dockerfile = (ROOT / "Dockerfile.web").read_text(encoding="utf-8")
    assert f"COPY infra/nginx-upstream-default/api.conf {NGINX_UPSTREAM_DIRECTORY}/api.conf" in web_dockerfile
    replica_env = dict(configured, VIBEFLOW_REPLICA_API_IMAGE='vibeflow/api:isolated-fixture')
    replicas = compose_config(replica_env, distributed=True, replicas=True)
    assert replicas.returncode == 0, replicas.stderr
    services = json.loads(replicas.stdout)['services']
    api, peer = services['api'], services['api-peer']
    assert api['hostname'] == 'api-primary' and peer['hostname'] == 'api-peer'
    assert api['networks']['default']['aliases'] == ['api-primary']
    assert not (peer['networks']['default'] or {}).get('aliases')
    assert {'api', 'api-peer'} == services['web']['depends_on'].keys()
    assert_upstream_mount(services["web"], replica_defaults)
    assert api['image'] == peer['image'] == services['arq-worker']['image'] == 'vibeflow/api:isolated-fixture'
    expected_routes = {'api-primary': 'http://api-primary:8000', 'api-peer': 'http://api-peer:8000'}
    for service in (api, peer):
        assert service['stop_grace_period'] == '1m30s'
        assert service['environment']['VIBEFLOW_DISTRIBUTED_JOBS'] == 'true'
        for variable in ('VIBEFLOW_TERMINAL_OWNER_ROUTES', 'VIBEFLOW_VIZ_OWNER_ROUTES'):
            assert json.loads(service['environment'][variable]) == expected_routes
    with tempfile.TemporaryDirectory(prefix="vf-nginx-upstreams-") as directory:
        override = compose_config(dict(replica_env, VIBEFLOW_REPLICA_UPSTREAM_DIR=directory),
                                  distributed=True, replicas=True)
        assert override.returncode == 0, override.stderr
        assert_upstream_mount(json.loads(override.stdout)["services"]["web"], Path(directory))
    print("15. 双 API 的入口配置目录、独占所属地址与稳定关闭时间 [OK]")
    print("\n=== 生产 compose 离线冒烟通过 ===")


if __name__ == "__main__":
    main()
