# Docker 修复记录

## 当前口径（2026-11-02）

旧文中的“路线彻底堵死”是 2026-10-22 的历史结论，不再用作当前判断。上一轮本机修复已使 Docker Engine 20.10.14 恢复运行；镜像拉取仍遇到仓库/网络错误，容器构建和业务闭环尚未验收。当前稳定化基准使用本机 PostgreSQL/fs 与 WSL OpenFOAM，Docker 不作为开发前置；compose 离线配置解析通过不等于容器部署已通过。

## 最终结论（2026-10-22，用户授权出厂重置后）

**Docker 在本机是双重硬卡点，候选部署产物已写出但本地未验证**：

1. **引擎建不出 VM**：出厂重置（`DockerCli.exe -Reset`，用户明示授权）后，docker-desktop WSL 发行版仍不创建；`\\.\pipe\dockerProcd` 特权服务管道始终缺失；com.docker.service 重启无效；10-20 VM 日志曾报 cgroup mount 错误。Docker Desktop 深度损坏，重装通道 desktop.docker.com **网络不可达（000）**。
2. **镜像也拉不了**：registry-1.docker.io / auth.docker.io / hub.docker.com 全部超时（000）——即便引擎修好，`postgres:16-alpine`/`seaweedfs`/`ubuntu:24.04` 一个都拉不下来。

**⇒ 本机 Docker 路线彻底堵死（引擎坏 + 镜像仓库不可达），不是投入产出问题。**

## 候选交付物（仍需目标机端到端验证）

- `infra/Dockerfile.api`（python3.11+全依赖+libGL 族）
- `infra/Dockerfile.web`（vite build → nginx 静态+反代）
- `infra/Dockerfile.sandbox`（Ubuntu24.04+OpenFOAM+TeX Live+非 root，方案 §Linux 沙箱形态）
- `infra/docker-compose.prod.yml`（postgres+seaweedfs+api+web 健康依赖链）
- `infra/nginx-web.conf`（/api /ws /viz-proxy 反代+WS 升级头）

建议的目标机验证路径：`docker compose -f infra/docker-compose.prod.yml up -d --build` → http://localhost:8080。

2026-10-24 离线核验：两份 compose 均可由 `docker compose config` 解析；`infra/compose_smoke.py` 五项通过（生产密钥强制、无开发默认值、前端完整 build、构建上下文排除、OpenFOAM 补全资产入镜像）。本机 `docker version` 仍因 engine pipe 不存在而失败，因此镜像构建和容器业务验收未完成。

**这不等于已验证可直接部署。** 运行前必须完成 `deployment.md` 的检查，至少包括：设置独立生产密钥；Alembic 建库/纳管；核对 S3 与本地路径混用；补 Linux 容器 PTY；确认 Redis 未接入时的单进程限制；验证镜像拉取、真实算例、viz/WS、备份与回滚。

## 平台不受影响

WSL 直连形态（vibeflow-linux）覆盖 Linux 沙箱诉求的全部开发功能；Docker 只是团队服务器部署形态。Docker Desktop 进程保持运行状态（未做不可逆操作之外的任何事）。

## 诊断结论（2026-10-20）

**根因**：`VirtualMachinePlatform`（虚拟机平台）Windows 功能被禁用 → WSL2 无法运行 → Docker Desktop 引擎永远起不来。

证据链：
- `vmcompute` / `WSLService` 服务不存在
- `wsl -l -v` 只剩遗留的 `docker-desktop-data`（Stopped），无 `docker-desktop` 引擎发行版
- 功能状态：VirtualMachinePlatform=Disabled，Microsoft-Hyper-V=Disabled，WSL=Enabled
- 硬件 OK：i7-6700K，BIOS 虚拟化已开（VirtualizationFirmwareEnabled=True，SLAT=True）→ 纯软件问题

## 已执行的修复

```powershell
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart   # 已成功
wsl --update                                                                            # 已安装 WSL 内核/服务
```

## 待用户操作

**必须重启 Windows** 功能才生效（hypervisor 随启动加载）。重启后：

```powershell
wsl --shutdown
# 启动 Docker Desktop，等鲸鱼图标变绿
docker version
cd platform\infra && docker compose up -d   # Postgres + Redis + SeaweedFS
```

若 Docker Desktop 仍报错且提到遗留 distro：
```powershell
wsl --unregister docker-desktop-data   # 清掉旧版遗留（DD 4.30+ 已不再使用它）
```

**2026-10-22 补注**：重启已做、出厂重置已做、服务重启已做——均无效；叠加镜像仓库不可达，本机 Docker 实质不可用。换到有网的机器或修复网络后再启用交付物。

