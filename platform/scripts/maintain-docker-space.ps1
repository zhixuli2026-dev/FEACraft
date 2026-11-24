param(
    [int]$MinimumFreeGB = 25,
    [string]$LogPath = 'D:\vibeflow_reverse\tmp\docker-space-maintenance.jsonl'
)
$ErrorActionPreference = 'Stop'
$entry = [ordered]@{ at = (Get-Date).ToString('o'); status = 'started'; minimumFreeGB = $MinimumFreeGB }
try {
    $entry.freeBytesBefore = (Get-PSDrive -Name C).Free
    if ($entry.freeBytesBefore -ge ($MinimumFreeGB * 1GB)) {
        $entry.status = 'skipped_enough_free_space'
    } else {
        $building = Get-CimInstance Win32_Process -Filter "Name='docker.exe' OR Name='docker-buildx.exe'" |
            Where-Object { $_.CommandLine -match '\s(build|bake)(\s|$)' }
        if ($building) {
            $entry.status = 'skipped_active_build'
        } else {
            $docker = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
            $localEngine = 'npipe:////./pipe/docker_engine'
            $probe = & $docker --host $localEngine version --format '{{.Server.Version}}' 2>$null
            if ($LASTEXITCODE -ne 0) { throw 'Docker engine unavailable' }
            $entry.engineVersion = "$probe".Trim()
            # Only regenerable cache and old dangling images; never prune volumes
            # or containers, and never restart Docker from this scheduled task.
            $cache = & $docker --host $localEngine builder prune --all --force --filter 'until=24h' --keep-storage '2GB' 2>&1
            if ($LASTEXITCODE -ne 0) { throw 'Docker build-cache cleanup failed' }
            $entry.cacheSummary = @($cache | Where-Object { "$_" -match 'reclaimed|Total' } | ForEach-Object { "$_" })
            $images = & $docker --host $localEngine image prune --force --filter 'until=72h' 2>&1
            if ($LASTEXITCODE -ne 0) { throw 'Docker dangling-image cleanup failed' }
            $entry.imageSummary = @($images | Where-Object { "$_" -match 'reclaimed|Total' } | ForEach-Object { "$_" })
            $entry.status = 'completed'
        }
    }
} catch {
    $entry.status = 'failed'
    $entry.error = $_.Exception.Message
} finally {
    $entry.freeBytesAfter = (Get-PSDrive -Name C).Free
    $directory = Split-Path -Parent $LogPath
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $entry | ConvertTo-Json -Compress -Depth 4 | Add-Content -LiteralPath $LogPath -Encoding UTF8
}
$entry | ConvertTo-Json -Depth 4
if ($entry.status -eq 'failed') { exit 1 }
