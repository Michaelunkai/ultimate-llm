#requires -Version 5.1
<#
.SYNOPSIS
    Prerequisite detection and optional installation for the ultimate-llm project.

.DESCRIPTION
    Frozen interface:
        function Test-Prerequisites            # report-only, installs NOTHING
        function Ensure-Prerequisites          # may install; idempotent; -AcceptInstalls switch

    Both return a PSCustomObject (ConvertTo-Json -Depth 5 safe) with keys:
        ollama { installed (bool), path (string|null), version (string|null), daemon_up (bool) }
        python { found (bool), version (string|null) }
        venv   { exists (bool), path (string) }
        winget { ok (bool) }
        disk   { free_gb (number) }

    CLI mode (self-execution):
        powershell -NoProfile -ExecutionPolicy Bypass -File lib\ensure-prereqs.ps1 -Report
        powershell -NoProfile -ExecutionPolicy Bypass -File lib\ensure-prereqs.ps1 -AcceptInstalls
    -File with no switches, or with -Report, prints the report JSON and exits 0 (safe default).
    Dot-sourcing this file only defines the functions and produces NO output.
#>

param(
    [switch]$Report,
    [switch]$AcceptInstalls
)

$script:UltimateRoot = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { 'F:\study\AI_ML\LocalAI\ultimate-llm' }
$script:VenvPath = Join-Path $script:UltimateRoot '.venv'

# ---------------------------------------------------------------------------
# Internal helpers (script-private)
# ---------------------------------------------------------------------------

function Get-PrereqOllamaPath {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($null -ne $cmd -and -not [string]::IsNullOrWhiteSpace("$($cmd.Source)")) {
        return "$($cmd.Source)"
    }
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    $candidate = 'C:\Program Files\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}

function Test-PrereqOllamaDaemon {
    param([AllowNull()][string]$OllamaExe)
    if ([string]::IsNullOrEmpty($OllamaExe) -or -not (Test-Path -LiteralPath $OllamaExe)) { return $false }
    $job = $null
    try {
        $job = Start-Job -ScriptBlock {
            param($exe)
            & $exe list 2>&1 | Out-Null
            $LASTEXITCODE
        } -ArgumentList $OllamaExe
        $finished = Wait-Job -Job $job -Timeout 10
        if ($null -ne $finished -and $job.State -eq 'Completed') {
            $code = Receive-Job -Job $job | Select-Object -Last 1
            return ("$code" -eq '0')
        }
        return $false
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $job) { Remove-Job -Job $job -Force -ErrorAction SilentlyContinue }
    }
}

function Get-PrereqPythonVersion {
    # Returns a version string ("3.14.0") or $null. Tries python, then py -3.
    foreach ($probe in @('python --version', 'py -3 --version')) {
        $exe = ($probe -split ' ')[0]
        if ($null -eq (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = (& cmd /c $probe 2>&1) | Out-String
            if ($out -match 'Python\s+([0-9][^\s\r\n]*)') { return $Matches[1] }
        }
        catch { }
    }
    return $null
}

function Reset-PrereqSessionPath {
    try {
        $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $env:Path = "$machinePath;$userPath"
    }
    catch { }
    $env:Path = "$env:Path;" + (Join-Path $env:LOCALAPPDATA 'Programs\Ollama') + ';C:\Program Files\Ollama'
}

function Invoke-PrereqWingetInstall {
    param([string]$Id)
    if ($null -eq (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    try {
        & winget install -e --id $Id --accept-source-agreements --accept-package-agreements --silent 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# Public frozen interface
# ---------------------------------------------------------------------------

function Test-Prerequisites {
    # Report-only detection. Installs NOTHING.
    $ollamaPath = Get-PrereqOllamaPath

    $ollamaVersion = $null
    if (-not [string]::IsNullOrEmpty($ollamaPath)) {
        try {
            $verOut = (& $ollamaPath --version 2>&1) | Out-String
            if ($verOut -match 'ollama version is\s+([0-9][^\s\r\n]*)') { $ollamaVersion = $Matches[1] }
        }
        catch { $ollamaVersion = $null }
    }

    $pyVersion = Get-PrereqPythonVersion

    $freeGb = [double]0
    try {
        $rootPath = (Resolve-Path -LiteralPath $script:UltimateRoot).ProviderPath
        $driveRoot = ([System.IO.Path]::GetPathRoot($rootPath)).TrimEnd('\')
        $disk = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$driveRoot'" -ErrorAction Stop
        if ($null -ne $disk) { $freeGb = [math]::Round(($disk.FreeSpace / 1GB), 2) }
    }
    catch { $freeGb = [double]0 }

    return [PSCustomObject]@{
        ollama = [PSCustomObject]@{
            installed = (-not [string]::IsNullOrEmpty($ollamaPath))
            path      = $ollamaPath
            version   = $ollamaVersion
            daemon_up = (Test-PrereqOllamaDaemon -OllamaExe $ollamaPath)
        }
        python = [PSCustomObject]@{
            found   = (-not [string]::IsNullOrEmpty($pyVersion))
            version = $pyVersion
        }
        venv = [PSCustomObject]@{
            exists = (Test-Path -LiteralPath (Join-Path $script:VenvPath 'Scripts\python.exe'))
            path   = $script:VenvPath
        }
        winget = [PSCustomObject]@{
            ok = ($null -ne (Get-Command winget -ErrorAction SilentlyContinue))
        }
        disk = [PSCustomObject]@{
            free_gb = $freeGb
        }
    }
}

function Ensure-Prerequisites {
    # May install missing prerequisites; idempotent. Without -AcceptInstalls: report only.
    [CmdletBinding()]
    param(
        [switch]$AcceptInstalls
    )

    $report = Test-Prerequisites
    if (-not $AcceptInstalls) { return $report }

    # 1) ollama executable
    if (-not $report.ollama.installed) {
        $installed = Invoke-PrereqWingetInstall -Id 'Ollama.Ollama'
        if (-not $installed) {
            try {
                $ProgressPreference = 'SilentlyContinue'
                [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
                $setup = Join-Path $env:TEMP 'OllamaSetup.exe'
                Invoke-WebRequest -Uri 'https://ollama.com/download/OllamaSetup.exe' -OutFile $setup -TimeoutSec 600 -UseBasicParsing
                $null = Start-Process -FilePath $setup -ArgumentList '/VERYSILENT', '/NORESTART' -Wait
                $installed = $true
            }
            catch { $installed = $false }
        }
        Reset-PrereqSessionPath
    }

    # 2) ollama daemon
    $ollamaPath = Get-PrereqOllamaPath
    if (-not [string]::IsNullOrEmpty($ollamaPath) -and -not (Test-PrereqOllamaDaemon -OllamaExe $ollamaPath)) {
        # Max-context opt-in (deliberate, documented in the llm skill + final
        # report): flash attention + q8_0 KV cache halve KV memory, which is
        # what makes the 65536 real-prompt window fit in 16GB VRAM. Persisted
        # user-level so daemon respawns (incl. the Ollama app after reboot)
        # inherit it too.
        try {
            $env:OLLAMA_FLASH_ATTENTION = '1'
            $env:OLLAMA_KV_CACHE_TYPE = 'q8_0'
            [Environment]::SetEnvironmentVariable('OLLAMA_FLASH_ATTENTION', '1', 'User')
            [Environment]::SetEnvironmentVariable('OLLAMA_KV_CACHE_TYPE', 'q8_0', 'User')
        }
        catch { }
        try {
            $null = Start-Process -FilePath $ollamaPath -ArgumentList 'serve' -WindowStyle Hidden
        }
        catch { }
        $deadline = (Get-Date).AddSeconds(30)
        do {
            if (Test-PrereqOllamaDaemon -OllamaExe $ollamaPath) { break }
            Start-Sleep -Seconds 2
        } while ((Get-Date) -lt $deadline)
        # CPU courtesy: the GPU-bound daemon stays below normal CPU priority.
        try {
            Get-Process -Name ollama,ollama_llama_server -ErrorAction SilentlyContinue | ForEach-Object { try { $_.PriorityClass = 'BelowNormal' } catch { } }
        }
        catch { }
    }

    # 3) python
    $pyVersion = Get-PrereqPythonVersion
    if ([string]::IsNullOrEmpty($pyVersion)) {
        $null = Invoke-PrereqWingetInstall -Id 'Python.Python.3.12'
        Reset-PrereqSessionPath
    }

    return (Test-Prerequisites)
}

# ---------------------------------------------------------------------------
# CLI mode (self-execution). Dot-sourcing stops here: functions only, no output.
# In -File mode $MyInvocation.Line is empty and InvocationName is the script
# path; when dot-sourced InvocationName is '.'. Both conditions are checked.
# ---------------------------------------------------------------------------
$script:DotSourced = ($MyInvocation.InvocationName -eq '.') -and (-not [string]::IsNullOrEmpty([string]$MyInvocation.Line))
if (-not $script:DotSourced) {
    if ($AcceptInstalls -and -not $Report) {
        $cliReport = Ensure-Prerequisites -AcceptInstalls
    }
    else {
        $cliReport = Test-Prerequisites
    }
    $cliReport | ConvertTo-Json -Depth 5
    exit 0
}
