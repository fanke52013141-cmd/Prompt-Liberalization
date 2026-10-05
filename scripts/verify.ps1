param(
    [ValidateSet('All', 'Main', 'Lite')][string]$Scope = 'All',
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$resultRoot = Join-Path $projectRoot '.verification-results'
New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null
$logPath = Join-Path $resultRoot ('offline-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
$env:PYTHONIOENCODING = 'utf-8'
Push-Location $projectRoot
try {
    $commands = @()
    if ($Scope -in @('All', 'Main')) {
        $commands += ,@('-m', 'pytest', 'tests/', '-q')
    }
    if ($Scope -in @('All', 'Lite')) {
        $commands += ,@('-m', 'unittest', 'discover', '-s', 'prompt-lab/tests', '-p', 'test_*.py')
        $commands += ,@('prompt-lab/tests/e2e_test.py')
    }
    foreach ($arguments in $commands) {
        # Windows PowerShell treats unittest's normal stderr as ErrorRecords.
        # Judge native execution by its exit code while preserving both streams.
        $previousErrorPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & python @arguments 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $logPath -Append
            $commandExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorPreference
        }
        if ($commandExit -ne 0) {
            throw "验证失败；日志：$logPath"
        }
    }
    Write-Output "离线验证通过；日志：$logPath"
} finally {
    Pop-Location
}
