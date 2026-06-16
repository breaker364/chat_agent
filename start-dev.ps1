Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$backendLogOut = Join-Path $root "backend_stdout.log"
$backendLogErr = Join-Path $root "backend_stderr.log"
$frontendLogOut = Join-Path $root "frontend_stdout.log"
$frontendLogErr = Join-Path $root "frontend_stderr.log"
$backendUrl = "http://127.0.0.1:8000/health"
$frontendUrl = "http://127.0.0.1:5173"
$envName = "env_311"
$envRoot = "C:\Users\hank.yu3\.conda\envs\env_311"
$backendArgs = @(
    "-m",
    "uvicorn",
    "backend.main:app",
    "--host",
    "127.0.0.1",
    "--port",
    "8000"
)

function Resolve-CommandPath {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Candidates
    )

    foreach ($candidate in $Candidates) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }

    return $null
}

function Resolve-PythonLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,
        [Parameter(Mandatory = $true)]
        [string]$EnvironmentRoot,
        [Parameter(Mandatory = $true)]
        [string]$EnvironmentName
    )

    $candidateDirs = @(
        $EnvironmentRoot,
        (Join-Path $ProjectRoot $EnvironmentName),
        (Join-Path (Split-Path -Parent $ProjectRoot) $EnvironmentName),
        (Join-Path (Split-Path -Parent (Split-Path -Parent $ProjectRoot)) $EnvironmentName),
        (Join-Path $env:USERPROFILE $EnvironmentName)
    ) | Select-Object -Unique

    foreach ($dir in $candidateDirs) {
        if (-not $dir) {
            continue
        }

        $pythonPath = Join-Path $dir "Scripts\python.exe"
        if (Test-Path $pythonPath) {
            return @{
                Mode = "python"
                FilePath = $pythonPath
                BaseArguments = @()
                Description = $pythonPath
            }
        }
    }

    $condaExe = Resolve-CommandPath -Candidates @("conda.exe", "conda")
    if ($condaExe) {
        return @{
            Mode = "conda"
            FilePath = $condaExe
            BaseArguments = @("run", "--no-capture-output", "-n", $EnvironmentName)
            Description = "conda environment '$EnvironmentName'"
        }
    }

    throw @"
Could not resolve Python environment '$EnvironmentName'.
Expected one of:
  1. $ProjectRoot\$EnvironmentName\Scripts\python.exe
  2. $(Split-Path -Parent $ProjectRoot)\$EnvironmentName\Scripts\python.exe
  3. A Conda environment named '$EnvironmentName'
"@
}

function Test-HttpReady {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url
    )

    try {
        $null = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $true
    } catch {
        return $false
    }
}

function Wait-ForHttpReady {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpReady -Url $Url) {
            Write-Host "$Name is ready: $Url"
            return $true
        }
        Start-Sleep -Milliseconds 500
    }

    Write-Warning "$Name did not become ready within $TimeoutSeconds seconds: $Url"
    return $false
}

function Start-BackgroundProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]
        [string]$StdOutPath,
        [Parameter(Mandatory = $true)]
        [string]$StdErrPath
    )

    return Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $StdOutPath `
        -RedirectStandardError $StdErrPath `
        -PassThru `
        -WindowStyle Hidden
}

if (-not (Test-Path $backendDir)) {
    throw "Backend directory not found: $backendDir"
}

if (-not (Test-Path $frontendDir)) {
    throw "Frontend directory not found: $frontendDir"
}

$pythonLauncher = Resolve-PythonLauncher -ProjectRoot $root -EnvironmentRoot $envRoot -EnvironmentName $envName

$npmExe = Resolve-CommandPath -Candidates @("npm.cmd", "npm")
if (-not $npmExe) {
    throw "Could not find npm in PATH."
}

if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
    throw "Frontend dependencies are missing. Run 'npm install' in $frontendDir first."
}

if (-not (Test-Path (Join-Path $backendDir "main.py"))) {
    throw "Backend entry file not found: $(Join-Path $backendDir 'main.py')"
}

if (-not (Test-HttpReady -Url $backendUrl)) {
    Write-Host "Starting backend with $($pythonLauncher.Description)..."
    $backendArgsToRun = @($pythonLauncher.BaseArguments + $backendArgs)
    $backendProcess = Start-BackgroundProcess `
        -FilePath $pythonLauncher.FilePath `
        -ArgumentList $backendArgsToRun `
        -WorkingDirectory $root `
        -StdOutPath $backendLogOut `
        -StdErrPath $backendLogErr
    Write-Host "Backend PID: $($backendProcess.Id)"
} else {
    Write-Host "Backend already running at $backendUrl"
}

if (-not (Test-HttpReady -Url $frontendUrl)) {
    Write-Host "Starting frontend..."
    $frontendArgs = @(
        "run",
        "dev",
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        "5173"
    )
    $frontendProcess = Start-BackgroundProcess `
        -FilePath $npmExe `
        -ArgumentList $frontendArgs `
        -WorkingDirectory $frontendDir `
        -StdOutPath $frontendLogOut `
        -StdErrPath $frontendLogErr
    Write-Host "Frontend PID: $($frontendProcess.Id)"
} else {
    Write-Host "Frontend already running at $frontendUrl"
}

$null = Wait-ForHttpReady -Url $backendUrl -Name "Backend" -TimeoutSeconds 30
$frontendReady = Wait-ForHttpReady -Url $frontendUrl -Name "Frontend" -TimeoutSeconds 30

if ($frontendReady) {
    Write-Host "Opening browser: $frontendUrl"
    Start-Process $frontendUrl
}

Write-Host ""
Write-Host "Logs:"
Write-Host "  Backend stdout: $backendLogOut"
Write-Host "  Backend stderr: $backendLogErr"
Write-Host "  Frontend stdout: $frontendLogOut"
Write-Host "  Frontend stderr: $frontendLogErr"
