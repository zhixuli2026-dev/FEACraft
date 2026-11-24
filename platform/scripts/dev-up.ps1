# VibeFlow Platform — 一键拉起全部开发服务（重启后用）
# 用法（PowerShell 或 Git Bash）：powershell -File platform\scripts\dev-up.ps1
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot

# -1. 只清理由本脚本 PID 文件记录的后端和历史 mock 残留。
#     PID 复用防护：可执行路径和预期命令行必须同时匹配。
$runDir = "$root\server\data\run"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$pythonExe = "$root\server\.venv\Scripts\python.exe"

function Start-Detached($name, $cmd, $argList, $workdir, $pidName = '') {
    $p = Start-Process -FilePath $cmd -ArgumentList $argList -WorkingDirectory $workdir `
          -WindowStyle Hidden -PassThru
    if ($pidName) { "$($p.Id)" | Set-Content -Encoding ascii "$runDir\$pidName.pid" }
    Write-Output "$name -> PID $($p.Id)"
}

$expectedPatterns = @{ backend = 'uvicorn app\.main'; 'mock-llm' = 'mock_llm\.py' }
foreach ($name in @('backend', 'mock-llm')) {
    $pidFile = "$runDir\$name.pid"
    if (-not (Test-Path $pidFile)) { continue }
    $processId = 0
    try { $processId = [int](Get-Content $pidFile -Raw).Trim() } catch {}
    if ($processId -gt 0) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
        if ($proc -and $proc.ExecutablePath -ieq $pythonExe -and
            $proc.CommandLine -match $expectedPatterns[$name]) {
            Write-Output "清理本脚本残留进程 PID $processId"
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# 0. 对象存储基础设施：fs 使用本机 Postgres，不启动 Docker/SeaweedFS。
$storageBackend = 'fs'
$envFile = "$root\server\.env"
if (Test-Path $envFile) {
    $storageLine = Get-Content $envFile |
        Where-Object { $_ -match '^\s*VIBEFLOW_STORAGE_BACKEND\s*=' } |
        Select-Object -First 1
    if ($storageLine) { $storageBackend = ($storageLine -split '=', 2)[1].Trim().Trim('"') }
}
if ($storageBackend -eq 's3') {
    $dockerOk = $false
    try {
        docker version --format '{{.Server.Version}}' 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $dockerOk = $true }
    } catch {}
    if ($dockerOk) {
        Write-Output 'Docker 引擎就绪，启动 compose 栈...'
        docker compose -f "$root\infra\docker-compose.yml" up -d
    } else {
        Write-Output 'Docker 引擎未就绪，S3 用 Windows 单二进制兜底'
        Start-Detached 'seaweedfs(S3)' "$root\infra\seaweedfs\weed.exe" `
            @('server','-s3','-s3.port=8333','-filer','-filer.port=8888','-dir=./s3data') "$root\infra"
    }
} else {
    Write-Output '存储后端 fs：使用本机 Postgres，跳过 Docker/SeaweedFS'
}

# 1. 后端
Start-Detached 'backend(FastAPI :8000)' "$pythonExe" `
    @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000') "$root\server" -PidName 'backend'

# 2. 前端；模型服务使用应用中已配置的真实 provider。
Start-Detached 'frontend(Vite :5173)' 'npm.cmd' @('run','dev') "$root\web"

Start-Sleep -Seconds 6
Write-Output ''
Write-Output '==== 健康检查 ===='
$ports = @(8000, 5173)
if ($storageBackend -eq 's3') { $ports = @(8333) + $ports }
foreach ($p in $ports) {
    try {
        $healthPath = if ($p -eq 8000) { '/api/health' } else { '/' }
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:$p$healthPath" -TimeoutSec 3 -UseBasicParsing
        Write-Output "$p OK"
    } catch {
        if ($_.Exception.Response) { Write-Output "$p OK (HTTP $([int]$_.Exception.Response.StatusCode))" }
        else { Write-Output "$p DOWN" }
    }
}
Write-Output ''
Write-Output '打开 http://localhost:5173'
