Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$runtimeConfigPath = Join-Path $root "runtime_config.json"
$runtimeConfig = @{}
if (Test-Path $runtimeConfigPath) {
    $runtimeConfig = Get-Content $runtimeConfigPath -Raw | ConvertFrom-Json
}
$appConfig = if ($runtimeConfig.PSObject.Properties.Name -contains "app") { $runtimeConfig.app } else { $null }
$pathConfig = if ($runtimeConfig.PSObject.Properties.Name -contains "paths") { $runtimeConfig.paths } else { $null }

function Get-ConfigValue {
    param(
        [Parameter(Mandatory = $false)]
        [object]$Section,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [object]$Default
    )

    if ($null -ne $Section -and $Section.PSObject.Properties.Name -contains $Name) {
        $value = $Section.$Name
        if ($null -ne $value -and "$value" -ne "") {
            return $value
        }
    }
    return $Default
}

$backendHost = [string](Get-ConfigValue -Section $appConfig -Name "backend_host" -Default "127.0.0.1")
$backendPort = [int](Get-ConfigValue -Section $appConfig -Name "backend_port" -Default 8000)
$frontendHost = [string](Get-ConfigValue -Section $appConfig -Name "frontend_host" -Default "127.0.0.1")
$frontendPort = [int](Get-ConfigValue -Section $appConfig -Name "frontend_port" -Default 5173)
$healthPath = [string](Get-ConfigValue -Section $appConfig -Name "backend_health_path" -Default "/health")
$pythonExecutable = [string](Get-ConfigValue -Section $pathConfig -Name "python_executable" -Default "")
$envName = [string](Get-ConfigValue -Section $pathConfig -Name "python_env_name" -Default "env_311")
$envRoot = [string](Get-ConfigValue -Section $pathConfig -Name "python_env_root" -Default "")
$backendLogOut = Join-Path $root "backend_stdout.log"
$backendLogErr = Join-Path $root "backend_stderr.log"
$frontendLogOut = Join-Path $root "frontend_stdout.log"
$frontendLogErr = Join-Path $root "frontend_stderr.log"
$backendUrl = "http://${backendHost}:${backendPort}${healthPath}"
$frontendUrl = "http://${frontendHost}:${frontendPort}"
$backendArgs = @(
    "-m",
    "uvicorn",
    "backend.main:app",
    "--host",
    $backendHost,
    "--port",
    "$backendPort"
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
        [Parameter(Mandatory = $false)]
        [AllowEmptyString()]
        [string]$PythonExecutable = "",
        [Parameter(Mandatory = $false)]
        [AllowEmptyString()]
        [string]$EnvironmentRoot = "",
        [Parameter(Mandatory = $true)]
        [string]$EnvironmentName
    )

    if ($PythonExecutable -and (Test-Path $PythonExecutable)) {
        return @{
            Mode = "python"
            FilePath = $PythonExecutable
            BaseArguments = @()
            Description = $PythonExecutable
        }
    }

    $candidateDirs = @(
        $EnvironmentRoot,
        $env:CONDA_PREFIX,
        $env:VIRTUAL_ENV,
        (Join-Path $ProjectRoot $EnvironmentName),
        (Join-Path (Split-Path -Parent $ProjectRoot) $EnvironmentName),
        (Join-Path (Split-Path -Parent (Split-Path -Parent $ProjectRoot)) $EnvironmentName),
        (Join-Path $env:USERPROFILE $EnvironmentName)
    ) | Where-Object { $_ -and "$_".Trim() } | Select-Object -Unique

    foreach ($dir in $candidateDirs) {
        if (-not $dir) {
            continue
        }

        $pythonCandidates = @(
            (Join-Path $dir "python.exe"),
            (Join-Path $dir "Scripts\python.exe")
        )
        foreach ($pythonPath in $pythonCandidates) {
            if (-not (Test-Path $pythonPath)) {
                continue
            }
            return @{
                Mode = "python"
                FilePath = $pythonPath
                BaseArguments = @()
                Description = $pythonPath
            }
        }
    }

    $pythonExe = Resolve-CommandPath -Candidates @("python.exe", "python", "py.exe", "py")
    if ($pythonExe) {
        return @{
            Mode = "python"
            FilePath = $pythonExe
            BaseArguments = @()
            Description = "Python from PATH: $pythonExe"
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

function Test-TcpPortListening {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    return @(Get-ListeningPidsOnPort -Port $Port).Count -gt 0
}

function Wait-ForPortListening {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPortListening -Port $Port) {
            Write-Host "$Name is listening on port $Port"
            return $true
        }
        Start-Sleep -Milliseconds 500
    }

    Write-Warning "$Name did not begin listening on port $Port within $TimeoutSeconds seconds."
    return $false
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

function Get-ListeningPidsOnPort {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        try {
            return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess -Unique)
        } catch {
            # Fall back to netstat below.
        }
    }

    $matches = netstat -ano -p tcp | Select-String "[:.]$Port\s+.*LISTENING\s+(\d+)$"
    $pids = @()
    foreach ($match in $matches) {
        if ($match.Matches.Count -gt 0) {
            $pids += [int]$match.Matches[0].Groups[1].Value
        }
    }
    return $pids | Select-Object -Unique
}

function Stop-ProcessesOnPort {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $pids = @(Get-ListeningPidsOnPort -Port $Port)
    if (-not $pids.Count) {
        return
    }

    Write-Host "Stopping existing $Name process(es) on port ${Port}: $($pids -join ', ')"
    foreach ($processId in $pids) {
        try {
            $null = Start-Process `
                -FilePath "taskkill.exe" `
                -ArgumentList @("/F", "/T", "/PID", "$processId") `
                -PassThru `
                -WindowStyle Hidden `
                -Wait
        } catch {
            Write-Warning "Failed to stop PID ${processId} on port ${Port}: $($_.Exception.Message)"
        }
    }

    $deadline = (Get-Date).AddSeconds(12)
    while ((Get-Date) -lt $deadline) {
        $remaining = @(Get-ListeningPidsOnPort -Port $Port)
        if (-not $remaining.Count) {
            return
        }
        Start-Sleep -Milliseconds 400
    }

    $remaining = @(Get-ListeningPidsOnPort -Port $Port)
    if ($remaining.Count) {
        Write-Warning "$Name port ${Port} is still occupied after kill attempt: $($remaining -join ', ')"
    }
}

if (-not (Test-Path $backendDir)) {
    throw "Backend directory not found: $backendDir"
}

if (-not (Test-Path $frontendDir)) {
    throw "Frontend directory not found: $frontendDir"
}

$pythonLauncher = Resolve-PythonLauncher `
    -ProjectRoot $root `
    -PythonExecutable $pythonExecutable `
    -EnvironmentRoot $envRoot `
    -EnvironmentName $envName

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

Stop-ProcessesOnPort -Port $backendPort -Name "backend"
Stop-ProcessesOnPort -Port $frontendPort -Name "frontend"

Write-Host "Starting backend with $($pythonLauncher.Description)..."
$backendArgsToRun = @($pythonLauncher.BaseArguments + $backendArgs)
$env:PYTHON_EXECUTABLE = $pythonLauncher.FilePath
$backendProcess = Start-BackgroundProcess `
    -FilePath $pythonLauncher.FilePath `
    -ArgumentList $backendArgsToRun `
    -WorkingDirectory $root `
    -StdOutPath $backendLogOut `
    -StdErrPath $backendLogErr
Write-Host "Backend PID: $($backendProcess.Id)"

Write-Host "Starting frontend..."
$frontendArgs = @(
    "run",
    "dev",
    "--",
    "--host",
    $frontendHost,
    "--port",
    "$frontendPort"
)
$frontendProcess = Start-BackgroundProcess `
    -FilePath $npmExe `
    -ArgumentList $frontendArgs `
    -WorkingDirectory $frontendDir `
    -StdOutPath $frontendLogOut `
    -StdErrPath $frontendLogErr
Write-Host "Frontend PID: $($frontendProcess.Id)"

$null = Wait-ForHttpReady -Url $backendUrl -Name "Backend" -TimeoutSeconds 30
$frontendReady = Wait-ForPortListening -Port $frontendPort -Name "Frontend" -TimeoutSeconds 30

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
