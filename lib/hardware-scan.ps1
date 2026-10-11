# =============================================================================
# hardware-scan.ps1
# Windows PowerShell 5.1 hardware probe for the ultimate-llm toolchain.
#
# Dot-source : defines Get-HardwareProfile (plus a private registry helper).
#              No output, no side effects.
# -File run  : prints the hardware profile as JSON to stdout, exit code 0.
#              Example: powershell -NoProfile -ExecutionPolicy Bypass -File lib/hardware-scan.ps1
#
# Notes:
#   - wmic is NOT used (absent on modern Windows 11); CIM cmdlets only.
#   - Every probe is wrapped in try/catch and degrades to $null on failure;
#     Get-HardwareProfile never throws.
# =============================================================================

function Get-GpuVramFromRegistry {
    # Exact VRAM lookup for one adapter via the display-class registry key.
    # Win32_VideoController.AdapterRAM is a 32-bit value capped at 4 GB, so the
    # registry value HardwareInformation.qwMemorySize is preferred.
    param([string]$ControllerName)

    try {
        if (-not $ControllerName) { return $null }
        $baseKey = "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        if (-not (Test-Path -LiteralPath $baseKey)) { return $null }

        $subKeys = Get-ChildItem -LiteralPath $baseKey -ErrorAction Stop
        foreach ($key in @($subKeys)) {
            try {
                $props = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop
                if (-not $props) { continue }

                $desc = $null
                try { $desc = [string]$props.DriverDesc } catch { $desc = $null }
                if (-not $desc) { continue }
                if ($desc -ine $ControllerName) { continue }

                $memRaw = $null
                try { $memRaw = $props.'HardwareInformation.qwMemorySize' } catch { $memRaw = $null }
                if (-not $memRaw) {
                    try { $memRaw = $props.'HardwareInformation.MemorySize' } catch { $memRaw = $null }
                }
                if (-not $memRaw) { continue }

                $memBytes = [double]0
                if ($memRaw -is [byte[]]) {
                    if ($memRaw.Length -ge 8) {
                        $memBytes = [double][BitConverter]::ToUInt64($memRaw, 0)
                    } elseif ($memRaw.Length -eq 4) {
                        $memBytes = [double][BitConverter]::ToUInt32($memRaw, 0)
                    }
                } else {
                    try { $memBytes = [double]$memRaw } catch { $memBytes = [double]0 }
                }

                if ($memBytes -gt 0) {
                    return [math]::Round($memBytes / 1GB, 1)
                }
            } catch { continue }
        }
    } catch { return $null }

    return $null
}

function Get-HardwareProfile {
    # Returns a PSCustomObject describing the host hardware and tool probes.
    # See file header for the JSON contract. Never throws.

    $gpus = @()

    # ---- GPUs: nvidia-smi first -------------------------------------------------
    try {
        $nvExe = $null
        try {
            $nvCmd = Get-Command -Name "nvidia-smi" -ErrorAction SilentlyContinue
            if ($nvCmd) {
                try { $nvExe = $nvCmd.Path } catch { $nvExe = $null }
                if (-not $nvExe) { try { $nvExe = $nvCmd.Source } catch { $nvExe = $null } }
            }
        } catch { $nvExe = $null }

        if (-not $nvExe) {
            $nvCandidates = @(
                (Join-Path $env:SystemRoot "System32\nvidia-smi.exe"),
                (Join-Path $env:ProgramFiles "NVIDIA Corporation\NVSMI\nvidia-smi.exe")
            )
            foreach ($cand in $nvCandidates) {
                try {
                    if ($cand -and (Test-Path -LiteralPath $cand -ErrorAction SilentlyContinue)) {
                        $nvExe = $cand
                        break
                    }
                } catch { }
            }
        }

        if ($nvExe) {
            $nvOut = $null
            try {
                $nvOut = & $nvExe --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>$null
            } catch { $nvOut = $null }

            $nvExit = 1
            try { $nvExit = [int]$LASTEXITCODE } catch { $nvExit = 1 }

            if ($nvExit -eq 0 -and $nvOut) {
                foreach ($line in @($nvOut)) {
                    try {
                        $lineStr = ([string]$line).Trim()
                        if (-not $lineStr) { continue }
                        $parts = $lineStr.Split(',')
                        if ($parts.Count -lt 2) { continue }

                        $gName = $parts[0].Trim()
                        $memTxt = $parts[1].Trim()
                        $gVram = $null
                        $m = [regex]::Match($memTxt, "(\d+(?:\.\d+)?)")
                        if ($m.Success) {
                            $mib = [double]$m.Groups[1].Value
                            if ($mib -gt 0) { $gVram = [math]::Round($mib / 1024.0, 1) }
                        }

                        $gpus += [pscustomobject]@{ name = $gName; vram_gb = $gVram; source = "nvidia-smi" }
                    } catch { }
                }
            }
        }
    } catch { }

    # ---- GPUs: CIM + registry fallback -------------------------------------------
    if ($gpus.Count -eq 0) {
        try {
            $controllers = Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop
            foreach ($vc in @($controllers)) {
                try {
                    $vName = $null
                    try { $vName = ([string]$vc.Name).Trim() } catch { $vName = $null }
                    if (-not $vName) { continue }

                    $vVram = $null
                    $vSource = "cim"

                    $regVram = Get-GpuVramFromRegistry -ControllerName $vName
                    if ($regVram -and ([double]$regVram) -gt 0) {
                        $vVram = $regVram
                        $vSource = "registry"
                    } else {
                        $adapterRam = [double]0
                        try { $adapterRam = [double]$vc.AdapterRAM } catch { $adapterRam = [double]0 }
                        if ($adapterRam -gt 0) {
                            $vVram = [math]::Round($adapterRam / 1GB, 1)
                            $vSource = "cim"
                        }
                    }

                    $gpus += [pscustomobject]@{ name = $vName; vram_gb = $vVram; source = $vSource }
                } catch { }
            }
        } catch { }
    }

    if ($gpus.Count -eq 0) {
        $gpus += [pscustomobject]@{ name = "none"; vram_gb = 0; source = "none" }
    }

    $vramTotal = [double]0
    foreach ($g in $gpus) {
        try {
            if ($null -ne $g.vram_gb) { $vramTotal += [double]$g.vram_gb }
        } catch { }
    }
    $vramTotal = [math]::Round($vramTotal, 1)

    # ---- CPU ----------------------------------------------------------------------
    $cpuName = $null
    $cpuCores = $null
    $cpuThreads = $null
    try {
        $cpu = Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop | Select-Object -First 1
        if ($cpu) {
            try { $cpuName = ([string]$cpu.Name).Trim() } catch { $cpuName = $null }
            try { if ($null -ne $cpu.NumberOfCores) { $cpuCores = [int]$cpu.NumberOfCores } } catch { $cpuCores = $null }
            try { if ($null -ne $cpu.NumberOfLogicalProcessors) { $cpuThreads = [int]$cpu.NumberOfLogicalProcessors } } catch { $cpuThreads = $null }
        }
    } catch { }

    # ---- RAM ----------------------------------------------------------------------
    $ramGb = $null
    try {
        $cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop | Select-Object -First 1
        if ($cs -and ($null -ne $cs.TotalPhysicalMemory)) {
            $ramGb = [math]::Round(([double]$cs.TotalPhysicalMemory) / 1GB, 1)
        }
    } catch { }

    # ---- OS -----------------------------------------------------------------------
    $osString = $null
    try {
        $osObj = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop | Select-Object -First 1
        if ($osObj) {
            $cap = $null
            try { $cap = ([string]$osObj.Caption).Trim() } catch { $cap = $null }
            $ver = $null
            try { $ver = ([string]$osObj.Version).Trim() } catch { $ver = $null }
            if ($cap -and $ver) { $osString = "$cap ($ver)" }
            elseif ($cap) { $osString = $cap }
            elseif ($ver) { $osString = $ver }
        }
    } catch { }

    # ---- Disk free on the drive this script lives on --------------------------------
    $diskFreeGb = $null
    try {
        $scriptDir = $null
        try { if ($PSScriptRoot) { $scriptDir = $PSScriptRoot } } catch { }
        if (-not $scriptDir) {
            try {
                if ($MyInvocation.ScriptName) { $scriptDir = Split-Path -Parent $MyInvocation.ScriptName }
            } catch { }
        }
        $driveLetter = $null
        if ($scriptDir) {
            try {
                $pathRoot = [System.IO.Path]::GetPathRoot($scriptDir)
                if ($pathRoot) { $driveLetter = $pathRoot.TrimEnd('\').TrimEnd(':') }
            } catch { }
        }
        if (-not $driveLetter) { $driveLetter = "C" }

        $psDrive = $null
        try { $psDrive = Get-PSDrive -Name $driveLetter -ErrorAction Stop } catch { $psDrive = $null }
        if (-not $psDrive -and ($driveLetter -ine "C")) {
            try { $psDrive = Get-PSDrive -Name "C" -ErrorAction Stop } catch { $psDrive = $null }
        }
        if ($psDrive -and ($null -ne $psDrive.Free)) {
            $diskFreeGb = [math]::Round(([double]$psDrive.Free) / 1GB, 1)
        }
    } catch { }

    # ---- Ollama ---------------------------------------------------------------------
    $ollamaPath = $null
    try {
        $ollamaCmd = Get-Command -Name "ollama" -ErrorAction SilentlyContinue
        if ($ollamaCmd) {
            try { $ollamaPath = $ollamaCmd.Path } catch { $ollamaPath = $null }
            if (-not $ollamaPath) { try { $ollamaPath = $ollamaCmd.Source } catch { $ollamaPath = $null } }
        }
    } catch { }
    if (-not $ollamaPath) {
        try {
            $ollamaCandidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
            if ($ollamaCandidate -and (Test-Path -LiteralPath $ollamaCandidate -ErrorAction SilentlyContinue)) {
                $ollamaPath = $ollamaCandidate
            }
        } catch { }
    }

    # ---- Python ----------------------------------------------------------------------
    $pythonVersion = $null
    try {
        $pyOut = $null
        try { $pyOut = cmd /c "python --version 2>&1" } catch { $pyOut = $null }
        if ($pyOut) {
            $pyText = ([string](@($pyOut) -join " ")).Trim()
            if ($pyText -match "Python\s+\d") {
                $pm = [regex]::Match($pyText, "\d+(?:\.\d+)+")
                if ($pm.Success) { $pythonVersion = $pm.Value } else { $pythonVersion = $pyText }
            }
        }
    } catch { }

    # ---- winget ----------------------------------------------------------------------
    $wingetOk = $false
    try {
        $wingetCmd = Get-Command -Name "winget" -ErrorAction SilentlyContinue
        if ($wingetCmd) { $wingetOk = $true }
    } catch { $wingetOk = $false }

    # ---- Assemble ----------------------------------------------------------------------
    $result = [pscustomobject]@{
        generated_at       = (Get-Date).ToString("o")
        os                 = $osString
        powershell_version = $PSVersionTable.PSVersion.ToString()
        cpu                = [pscustomobject]@{
            name    = $cpuName
            cores   = $cpuCores
            threads = $cpuThreads
        }
        ram_gb             = $ramGb
        gpus               = @($gpus)
        vram_total_gb      = $vramTotal
        disk_free_gb       = $diskFreeGb
        ollama_path        = $ollamaPath
        python_version     = $pythonVersion
        winget_ok          = $wingetOk
    }

    return $result
}

# =============================================================================
# Entry point: emit JSON only when this file is run directly (powershell -File).
# With -File, $MyInvocation.InvocationName is the path as typed on the command
# line; a relative path is resolved against the process cwd before comparing.
# Dot-sourcing sets InvocationName to "." (or "&" for the call operator), so
# those paths never match and nothing is printed.
# =============================================================================

$__hwIsMain = $false
try {
    $__hwSelfPath = $MyInvocation.MyCommand.Path
    $__hwInvName = $MyInvocation.InvocationName
    if ($__hwSelfPath -and $__hwInvName) {
        if ($__hwInvName -ieq $__hwSelfPath) {
            $__hwIsMain = $true
        } else {
            $__hwResolved = $null
            try { $__hwResolved = (Resolve-Path -LiteralPath $__hwInvName -ErrorAction Stop).Path } catch { $__hwResolved = $null }
            if ($__hwResolved -and ($__hwResolved -ieq $__hwSelfPath)) { $__hwIsMain = $true }
        }
    }
} catch { $__hwIsMain = $false }

if ($__hwIsMain) {
    try {
        $__hwProfile = Get-HardwareProfile
        $__hwProfile | ConvertTo-Json -Depth 6
        exit 0
    } catch {
        try {
            [pscustomobject]@{
                generated_at       = (Get-Date).ToString("o")
                os                 = $null
                powershell_version = $PSVersionTable.PSVersion.ToString()
                cpu                = [pscustomobject]@{ name = "probe-failed"; cores = $null; threads = $null }
                ram_gb             = $null
                gpus               = @([pscustomobject]@{ name = "none"; vram_gb = 0; source = "none" })
                vram_total_gb      = 0
                disk_free_gb       = $null
                ollama_path        = $null
                python_version     = $null
                winget_ok          = $false
            } | ConvertTo-Json -Depth 6
        } catch { }
        exit 0
    }
} else {
    Remove-Variable -Name __hwIsMain, __hwSelfPath, __hwInvName, __hwResolved -ErrorAction SilentlyContinue
}
