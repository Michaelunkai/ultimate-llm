# =============================================================================
# Start-UltimateAI.ps1
# One-shot bootstrap + launcher for the ultimate-llm toolchain.
#
# Windows PowerShell 5.1 compatible (no ternary, no null-coalescing).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File Start-UltimateAI.ps1
#       [-SelfTest] [-SkipPull] [-Model <tag>] [-Offline] [-LaunchOnly] [-NoAlias]
#
#   -SelfTest    fully offline self-test only (no pull, no install, no venv)
#   -SkipPull    do not check / pull the ollama model
#   -Model       use this ollama tag instead of hardware-aware discovery
#   -Offline     force offline model discovery
#   -LaunchOnly  skip prereqs / pull / venv / alias, jump straight to launch
#   -NoAlias     do not write the ultimate-ai.cmd shim / user PATH entry
#   -Full        force the complete bootstrap (discovery, prereqs, alias) and
#                refresh the cached fast-launch state
#
# Once a full bootstrap has succeeded once, normal runs take the FAST PATH:
# interactive mode is up in well under one second, with the model warming in the
# background (60-minute keep-alive) so the first message is instantly served.
# =============================================================================

param( [switch]$SelfTest, [switch]$SkipPull, [string]$Model, [switch]$Offline,
       [switch]$LaunchOnly, [switch]$NoAlias, [switch]$Full )

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$stateFile = Join-Path $PSScriptRoot '.bootstrap-state.json'
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$harnessFile = Join-Path $PSScriptRoot 'harness\harness.py'
$fastLaunch = $false
$state = $null

# ------------------------------------------------------------------ banner --
Write-Host ''
Write-Host '  ==============================================================' -ForegroundColor DarkCyan
Write-Host '     U L T I M A T E   A I   B O O T S T R A P' -ForegroundColor Cyan
Write-Host '  ==============================================================' -ForegroundColor DarkCyan
Write-Host ''

# ------------------------------------------------------- helper: big print --
function Write-BigChoice {
    param([string]$Tag, [string]$FitMode, [string]$Reason)
    Write-Host ''
    Write-Host '  ################################################################' -ForegroundColor Cyan
    Write-Host ('   CHOSEN MODEL : ' + $Tag) -ForegroundColor Yellow
    Write-Host ('   FIT MODE     : ' + $FitMode) -ForegroundColor Yellow
    if (-not [string]::IsNullOrEmpty($Reason)) {
        Write-Host ('   WHY          : ' + $Reason) -ForegroundColor Gray
    }
    Write-Host '  ################################################################' -ForegroundColor Cyan
    Write-Host ''
}

# --------------------------------------------------------------- dot-source --
# (deferred: only the paths that actually need the libs load them - the fast
#  path must stay lean to keep interactive startup under one second)

# =============================================================================
# SELF TEST (fully offline: no pull, no install, no venv creation)
# =============================================================================
if ($SelfTest) {
    try {
        . (Join-Path $PSScriptRoot 'lib\hardware-scan.ps1')
        . (Join-Path $PSScriptRoot 'lib\discover-model.ps1')
        . (Join-Path $PSScriptRoot 'lib\ensure-prereqs.ps1')
    }
    catch {
        Write-Host ('[ERROR] failed to load lib modules: ' + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }
    $failCount = 0

    # -- 1) parse-check every .ps1 under the deliverable root (incl. this file)
    $parseOk = $true
    $parseDetail = ''
    $ps1Files = @()
    try {
        $ps1Files = @(Get-ChildItem -LiteralPath $PSScriptRoot -Recurse -Filter '*.ps1' -File -ErrorAction SilentlyContinue)
    }
    catch { $ps1Files = @() }
    foreach ($f in $ps1Files) {
        $tok = $null
        $errs = $null
        try {
            [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tok, [ref]$errs)
        }
        catch {
            $errs = @()
            $parseOk = $false
            $parseDetail = $parseDetail + '[' + $f.Name + '] ' + $_.Exception.Message + '; '
        }
        if ($null -ne $errs -and @($errs).Count -gt 0) {
            $parseOk = $false
            foreach ($e in @($errs)) {
                $parseDetail = $parseDetail + '[' + $f.Name + ':' + $e.Extent.StartLineNumber + '] ' + $e.Message + '; '
            }
        }
    }
    if ($parseOk) {
        Write-Host '[SELFTEST] parse-all-ps1 PASS' -ForegroundColor Green
    }
    else {
        $failCount++
        Write-Host ('[SELFTEST] parse-all-ps1 FAIL ' + $parseDetail) -ForegroundColor Red
    }

    # -- 2) hardware profile (ram_gb + gpus + cpu.name)
    $hw = $null
    try { $hw = Get-HardwareProfile } catch { $hw = $null }
    $hwOk = $false
    $hwDetail = ''
    if ($null -ne $hw) {
        $hasRam = ($null -ne $hw.PSObject.Properties['ram_gb'] -and $null -ne $hw.ram_gb)
        $hasGpus = ($null -ne $hw.PSObject.Properties['gpus'] -and @($hw.gpus).Count -ge 1)
        $hasCpuName = $false
        if ($null -ne $hw.PSObject.Properties['cpu'] -and $null -ne $hw.cpu -and $null -ne $hw.cpu.PSObject.Properties['name']) {
            if (-not [string]::IsNullOrWhiteSpace([string]$hw.cpu.name)) { $hasCpuName = $true }
        }
        if ($hasRam -and $hasGpus -and $hasCpuName) { $hwOk = $true }
        else { $hwDetail = 'profile missing ram_gb / gpus / cpu.name' }
    }
    else { $hwDetail = 'Get-HardwareProfile returned null' }
    if ($hwOk) {
        Write-Host '[SELFTEST] hardware-profile PASS' -ForegroundColor Green
    }
    else {
        $failCount++
        Write-Host ('[SELFTEST] hardware-profile FAIL ' + $hwDetail) -ForegroundColor Red
    }

    # -- 3) offline model selection (model + fit + runner)
    $choice = $null
    try { $choice = Select-UltimateModel -HardwareProfile $hw -Offline } catch { $choice = $null }
    $selOk = $false
    $selDetail = ''
    if ($null -ne $choice) {
        $m = [string]$choice['model']
        $fit = $choice['fit']
        $run = $choice['runner']
        if ((-not [string]::IsNullOrEmpty($m)) -and ($null -ne $fit) -and ($null -ne $run)) { $selOk = $true }
        else { $selDetail = 'choice missing model / fit / runner' }
    }
    else { $selDetail = 'Select-UltimateModel returned null' }
    if ($selOk) {
        Write-Host '[SELFTEST] model-selection PASS' -ForegroundColor Green
    }
    else {
        $failCount++
        Write-Host ('[SELFTEST] model-selection FAIL ' + $selDetail) -ForegroundColor Red
    }

    # -- 4) prerequisites report (report-only, installs nothing)
    $prereq = $null
    try { $prereq = Test-Prerequisites } catch { $prereq = $null }
    $preOk = $false
    $preDetail = ''
    if ($null -ne $prereq) {
        $hasO = ($null -ne $prereq.PSObject.Properties['ollama'])
        $hasP = ($null -ne $prereq.PSObject.Properties['python'])
        $hasW = ($null -ne $prereq.PSObject.Properties['winget'])
        if ($hasO -and $hasP -and $hasW) { $preOk = $true }
        else { $preDetail = 'report missing ollama / python / winget keys' }
    }
    else { $preDetail = 'Test-Prerequisites returned null' }
    if ($preOk) {
        Write-Host '[SELFTEST] prerequisites PASS' -ForegroundColor Green
    }
    else {
        $failCount++
        Write-Host ('[SELFTEST] prerequisites FAIL ' + $preDetail) -ForegroundColor Red
    }

    # -- 5) harness offline self-test via system python (python, then py -3)
    $hOk = $false
    $hDetail = ''
    Push-Location $PSScriptRoot
    try {
        $pyCmd = Get-Command python -ErrorAction SilentlyContinue
        if ($null -ne $pyCmd) {
            & python harness\harness.py --self-test
            if ($LASTEXITCODE -eq 0) { $hOk = $true }
            else { $hDetail = 'python exit code ' + $LASTEXITCODE }
        }
        else { $hDetail = 'python not found' }
        if (-not $hOk) {
            $pyL = Get-Command py -ErrorAction SilentlyContinue
            if ($null -ne $pyL) {
                & py -3 harness\harness.py --self-test
                if ($LASTEXITCODE -eq 0) { $hOk = $true; $hDetail = '' }
                else {
                    if ($hDetail) { $hDetail = $hDetail + '; ' }
                    $hDetail = $hDetail + 'py -3 exit code ' + $LASTEXITCODE
                }
            }
            else {
                if (-not $hDetail) { $hDetail = 'py launcher not found' }
            }
        }
    }
    catch {
        $hOk = $false
        if (-not $hDetail) { $hDetail = $_.Exception.Message }
    }
    finally {
        Pop-Location
    }
    if ($hOk) {
        Write-Host '[SELFTEST] harness-self-test PASS' -ForegroundColor Green
    }
    else {
        $failCount++
        Write-Host ('[SELFTEST] harness-self-test FAIL ' + $hDetail) -ForegroundColor Red
    }

    # -- 6) harness\requirements.txt exists
    $reqPath = Join-Path $PSScriptRoot 'harness\requirements.txt'
    $reqOk = (Test-Path -LiteralPath $reqPath)
    $reqDetail = ''
    if (-not $reqOk) { $reqDetail = 'missing: ' + $reqPath }
    if ($reqOk) {
        Write-Host '[SELFTEST] requirements-txt PASS' -ForegroundColor Green
    }
    else {
        $failCount++
        Write-Host ('[SELFTEST] requirements-txt FAIL ' + $reqDetail) -ForegroundColor Red
    }

    Write-Host ''
    if ($failCount -eq 0) {
        Write-Host '[SELFTEST] all checks passed' -ForegroundColor Green
        exit 0
    }
    else {
        Write-Host ('[SELFTEST] ' + $failCount + ' check(s) FAILED') -ForegroundColor Red
        exit 1
    }
}

# =============================================================================
# FAST PATH - cached bootstrap: interactive mode in well under one second.
# Taken automatically when a previous full bootstrap succeeded (state file) and
# no explicit setup flag requests otherwise. -Full forces the complete path.
# =============================================================================
if ((-not $SelfTest) -and (-not $LaunchOnly) -and (-not $Full) -and (-not $Offline) -and
    [string]::IsNullOrWhiteSpace($Model) -and (Test-Path -LiteralPath $stateFile) -and
    (Test-Path -LiteralPath $venvPython) -and (Test-Path -LiteralPath $harnessFile)) {
    $state = $null
    try { $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json } catch { $state = $null }
    if ($null -ne $state -and -not [string]::IsNullOrWhiteSpace([string]$state.model)) {
        $tag = [string]$state.model
        try { $numCtx = [int]$state.num_ctx } catch { $numCtx = 0 }
        $fastLaunch = $true
        try {
            if ($null -ne $state.hw) { $env:ULTIMATEAI_HW = ($state.hw | ConvertTo-Json -Compress) }
        } catch { }
        # Warm the model in the background (detached; 60-minute keep-alive) so the
        # very first message does not wait for the weights to load.
        try {
            $warmCmd = "Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:11434/api/generate' -ContentType 'application/json' -TimeoutSec 300 -Body (@{ model='" + $tag + "'; prompt=''; keep_alive='60m'; stream=`$false } | ConvertTo-Json) | Out-Null"
            Start-Process -WindowStyle Hidden -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-Command', $warmCmd) | Out-Null
        }
        catch { }
        Write-Host ('[READY] fast launch: ' + $tag + ' - bootstrap cached (run with -Full to redo discovery/setup).') -ForegroundColor Cyan
    }
}

# =============================================================================
# NORMAL FLOW
# =============================================================================

$fitMode = ''
$reason = ''
$choice = $null

if ($fastLaunch) {
    # fast path: $tag / $numCtx already resolved from cache - go straight to launch
}
elseif ($LaunchOnly) {
    # ------------------------------------------------ launch-only short path --
    try {
        . (Join-Path $PSScriptRoot 'lib\hardware-scan.ps1')
        . (Join-Path $PSScriptRoot 'lib\discover-model.ps1')
        . (Join-Path $PSScriptRoot 'lib\ensure-prereqs.ps1')
    }
    catch {
        Write-Host ('[ERROR] failed to load lib modules: ' + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }
    if (-not [string]::IsNullOrWhiteSpace($Model)) {
        $tag = $Model.Trim()
        Write-Host ('[LAUNCH-ONLY] model tag from -Model: ' + $tag)
    }
    else {
        Write-Host '[LAUNCH-ONLY] no -Model given - selecting via OFFLINE discovery ...'
        $hw = $null
        try { $hw = Get-HardwareProfile } catch { $hw = $null }
        try { $choice = Select-UltimateModel -HardwareProfile $hw -Offline } catch { $choice = $null }
        if ($null -eq $choice -or [string]::IsNullOrEmpty([string]$choice['model'])) {
            Write-Host '[ERROR] offline model discovery failed - pass -Model <tag> with -LaunchOnly.' -ForegroundColor Red
            exit 1
        }
        $tag = [string]$choice['model']
        $numCtx = [int]$choice['runner']['num_ctx']
        $fitMode = [string]$choice['fit']['mode']
        $reason = [string]$choice['reason']
        Write-BigChoice -Tag $tag -FitMode $fitMode -Reason $reason
    }
    Write-Host '[LAUNCH-ONLY] skipping prereqs / pull / venv / alias - jumping to launch.'
}
else {
    # ------------------------------------------------------- prerequisites --
    try {
        . (Join-Path $PSScriptRoot 'lib\hardware-scan.ps1')
        . (Join-Path $PSScriptRoot 'lib\discover-model.ps1')
        . (Join-Path $PSScriptRoot 'lib\ensure-prereqs.ps1')
    }
    catch {
        Write-Host ('[ERROR] failed to load lib modules: ' + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }
    try {
        $prereq = Ensure-Prerequisites -AcceptInstalls
        Write-Host ('[PREREQ] ' + ($prereq | ConvertTo-Json -Compress -Depth 5))
    }
    catch {
        Write-Host ('[ERROR] prerequisite stage failed: ' + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }

    $ollamaExe = ''
    $oc = Get-Command ollama -ErrorAction SilentlyContinue
    if ($null -ne $oc) {
        $ocSrc = ''
        try { $ocSrc = [string]$oc.Source } catch { $ocSrc = '' }
        if ([string]::IsNullOrWhiteSpace($ocSrc)) { $ollamaExe = 'ollama' } else { $ollamaExe = $ocSrc }
    }
    if (([string]::IsNullOrWhiteSpace($ollamaExe) -or $ollamaExe -eq 'ollama') -and ($null -ne $prereq) -and ($null -ne $prereq.ollama)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$prereq.ollama.path)) { $ollamaExe = [string]$prereq.ollama.path }
    }
    if ([string]::IsNullOrWhiteSpace($ollamaExe) -or ($ollamaExe -ne 'ollama' -and -not (Test-Path -LiteralPath $ollamaExe))) {
        Write-Host '[ERROR] Ollama is missing even after Ensure-Prerequisites.' -ForegroundColor Red
        Write-Host '        Re-run Start-UltimateAI.ps1 to retry the install, or install manually:' -ForegroundColor Red
        Write-Host '        winget install -e --id Ollama.Ollama' -ForegroundColor Red
        exit 1
    }

    # --------------------------------------------------------------- model --
    if (-not [string]::IsNullOrWhiteSpace($Model)) {
        $tag = $Model.Trim()
        Write-Host ('[MODEL] using -Model tag: ' + $tag)
    }
    else {
        $hw = $null
        try { $hw = Get-HardwareProfile } catch { $hw = $null }
        try { $choice = Select-UltimateModel -HardwareProfile $hw -Offline:$Offline } catch { $choice = $null }
        if ($null -eq $choice -or [string]::IsNullOrEmpty([string]$choice['model'])) {
            Write-Host '[MODEL] online discovery failed or nothing fits - falling back to OFFLINE catalog ...' -ForegroundColor Yellow
            try { $choice = Select-UltimateModel -HardwareProfile $hw -Offline } catch { $choice = $null }
        }
        if ($null -eq $choice -or [string]::IsNullOrEmpty([string]$choice['model'])) {
            Write-Host '[ERROR] model discovery failed (online and offline fallback).' -ForegroundColor Red
            exit 1
        }
        $tag = [string]$choice['model']
        $numCtx = [int]$choice['runner']['num_ctx']
        $fitMode = [string]$choice['fit']['mode']
        $reason = [string]$choice['reason']
        Write-BigChoice -Tag $tag -FitMode $fitMode -Reason $reason
    }

    # --------------------------------------------------------------- pull ---
    if (-not $SkipPull) {
        $haveModel = $false
        try {
            $null = & $ollamaExe show $tag 2>&1
            $haveModel = ($LASTEXITCODE -eq 0)
        }
        catch { $haveModel = $false }
        if ($haveModel) {
            Write-Host ('[PULL] "' + $tag + '" already present locally - skipping pull.')
        }
        else {
            Write-Host ('[PULL] pulling "' + $tag + '" (live output) ...')
            & $ollamaExe pull $tag
            if ($LASTEXITCODE -ne 0) {
                Write-Host ('[ERROR] ollama pull failed with exit code ' + $LASTEXITCODE) -ForegroundColor Red
                exit 1
            }
        }
    }
    else {
        Write-Host '[PULL] -SkipPull given - skipping pull check.'
    }

    # --------------------------------------------------------------- venv ---
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host '[VENV] .venv not found - creating virtual environment ...'
        $venvDir = Join-Path $PSScriptRoot '.venv'
        $made = $false
        try {
            if ($null -ne (Get-Command python -ErrorAction SilentlyContinue)) {
                & python -m venv $venvDir
                $made = (($LASTEXITCODE -eq 0) -and (Test-Path -LiteralPath $venvPython))
            }
        }
        catch { $made = $false }
        if (-not $made) {
            try {
                if ($null -ne (Get-Command py -ErrorAction SilentlyContinue)) {
                    & py -3 -m venv $venvDir
                    $made = (($LASTEXITCODE -eq 0) -and (Test-Path -LiteralPath $venvPython))
                }
            }
            catch { $made = $false }
        }
        if (-not $made) {
            Write-Host '[ERROR] could not create .venv (tried python -m venv and py -3 -m venv).' -ForegroundColor Red
            exit 1
        }
    }

    $depsOk = $false
    try {
        & $venvPython -c "import rich, httpx"
        $depsOk = ($LASTEXITCODE -eq 0)
    }
    catch { $depsOk = $false }
    if (-not $depsOk) {
        Write-Host '[DEPS] installing harness requirements into .venv ...'
        try {
            & $venvPython -m pip install -r (Join-Path $PSScriptRoot 'harness\requirements.txt') --disable-pip-version-check -q
        }
        catch {
            Write-Host ('[DEPS] pip install raised: ' + $_.Exception.Message) -ForegroundColor Yellow
        }
        $verifyOk = $false
        try {
            & $venvPython -c "import rich, httpx"
            $verifyOk = ($LASTEXITCODE -eq 0)
        }
        catch { $verifyOk = $false }
        if (-not $verifyOk) {
            Write-Host '[ERROR] .venv is missing required packages (rich, httpx) after pip install.' -ForegroundColor Red
            Write-Host '        Fix manually: .venv\Scripts\python.exe -m pip install -r harness\requirements.txt' -ForegroundColor Red
            exit 1
        }
        Write-Host '[DEPS] rich + httpx verified in .venv.'
    }
    else {
        Write-Host '[DEPS] .venv already has rich + httpx - skipping pip.'
    }

    # optional extras (best-effort, failures ignored with a notice)
    foreach ($pkg in @('playwright', 'ddgs', 'beautifulsoup4')) {
        try {
            & $venvPython -m pip install $pkg --disable-pip-version-check -q
            if ($LASTEXITCODE -ne 0) {
                Write-Host ('[DEPS] optional package not installed (ignored): ' + $pkg) -ForegroundColor Yellow
            }
        }
        catch {
            Write-Host ('[DEPS] optional package not installed (ignored): ' + $pkg) -ForegroundColor Yellow
        }
    }

    # -------------------------------------------------------------- alias ---
    if (-not $NoAlias) {
        try {
            $binDir = Join-Path $env:LOCALAPPDATA 'UltimateAI\bin'
            if (-not (Test-Path -LiteralPath $binDir)) {
                New-Item -ItemType Directory -Path $binDir -Force -ErrorAction Stop | Out-Null
            }
            $cmdTarget = Join-Path $PSScriptRoot 'Ultimate-AI.cmd'
            $shimPath = Join-Path $binDir 'ultimate-ai.cmd'
            $shim = '@echo off' + "`r`n" + ('call "' + $cmdTarget + '" %*') + "`r`n"
            [System.IO.File]::WriteAllText($shimPath, $shim, (New-Object System.Text.ASCIIEncoding))
            Write-Host ('[ALIAS] shim written: ' + $shimPath)

            $userPath = ''
            try { $userPath = [string][Environment]::GetEnvironmentVariable('Path', 'User') } catch { $userPath = '' }
            $inPath = $false
            foreach ($piece in ($userPath -split ';')) {
                if ($piece.Trim().TrimEnd('\') -ieq $binDir.TrimEnd('\')) { $inPath = $true; break }
            }
            if (-not $inPath) {
                $newPath = $userPath
                if ($newPath.EndsWith(';')) { $newPath = $newPath + $binDir }
                else { $newPath = $newPath + ';' + $binDir }
                [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
                Write-Host '[ALIAS] user PATH updated (applies to NEW terminals only).' -ForegroundColor Yellow
            }
        }
        catch {
            Write-Host ('[ALIAS] warning: could not install shim: ' + $_.Exception.Message) -ForegroundColor Red
        }
    }

    # ------------------------------------------------- cache bootstrap state --
    # Successful full bootstrap: cache the chosen model so future launches take
    # the fast path (interactive in well under one second). Refreshed by -Full.
    try {
        $numCtxOut = 0
        if ($null -ne $numCtx) { $numCtxOut = [int]$numCtx }
        $hwOut = $null
        try {
            if ($null -ne $hw) {
                $firstGpu = $hw.gpus | Select-Object -First 1
                $hwOut = @{
                    gpu = [string]$firstGpu.name
                    vram_gb = [double]$hw.vram_total_gb
                    ram_gb = [double]$hw.ram_gb
                    cpu = [string]$hw.cpu.name
                }
            }
        } catch { $hwOut = $null }
        $stateOut = @{ model = $tag; num_ctx = $numCtxOut; hw = $hwOut; saved_at = (Get-Date -Format 'o') }
        ($stateOut | ConvertTo-Json -Compress) | Set-Content -LiteralPath $stateFile -Encoding ASCII
        Write-Host ('[STATE] bootstrap cached for fast launches: ' + $stateFile)
    }
    catch {
        Write-Host ('[STATE] warning: could not cache bootstrap state: ' + $_.Exception.Message) -ForegroundColor Yellow
    }
}

# ============================================================== LAUNCH =======
if (-not $env:ULTIMATEAI_HW) {
    try {
        if ($null -ne $state -and $null -ne $state.hw) {
            $env:ULTIMATEAI_HW = ($state.hw | ConvertTo-Json -Compress)
        }
        elseif (-not $fastLaunch) {
            $hwL = Get-HardwareProfile
            $firstGpuL = $hwL.gpus | Select-Object -First 1
            $env:ULTIMATEAI_HW = (@{
                gpu = [string]$firstGpuL.name
                vram_gb = [double]$hwL.vram_total_gb
                ram_gb = [double]$hwL.ram_gb
                cpu = [string]$hwL.cpu.name
            } | ConvertTo-Json -Compress)
        }
    } catch { }
}
$pyExe = ''
$pyPre = @()
if (Test-Path -LiteralPath $venvPython) {
    $pyExe = $venvPython
}
else {
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pyCmd) {
        $pySrc = ''
        try { $pySrc = [string]$pyCmd.Source } catch { $pySrc = '' }
        if ([string]::IsNullOrWhiteSpace($pySrc)) { $pyExe = 'python' } else { $pyExe = $pySrc }
    }
    else {
        $pyL = Get-Command py -ErrorAction SilentlyContinue
        if ($null -ne $pyL) {
            $pyLSrc = ''
            try { $pyLSrc = [string]$pyL.Source } catch { $pyLSrc = '' }
            if ([string]::IsNullOrWhiteSpace($pyLSrc)) { $pyExe = 'py' } else { $pyExe = $pyLSrc }
            $pyPre = @('-3')
        }
    }
}
if ([string]::IsNullOrEmpty($pyExe)) {
    Write-Host '[ERROR] no Python found (venv, python, py -3) - cannot launch harness.' -ForegroundColor Red
    exit 1
}

$harnessPath = Join-Path $PSScriptRoot 'harness\harness.py'
$launchArgs = @()
$launchArgs += $pyPre
$launchArgs += $harnessPath
$launchArgs += @('--model', $tag, '--yes')
if ($null -ne $numCtx -and ([int]$numCtx) -gt 0) {
    $launchArgs += @('--num-ctx', ([string]$numCtx))
}

Write-Host ''
Write-Host ('[LAUNCH] ' + $pyExe + ' ' + ($launchArgs -join ' ')) -ForegroundColor Cyan
Write-Host ('[READY] interactive mode starting - total startup ' + $sw.ElapsedMilliseconds + ' ms.') -ForegroundColor Green
Write-Host ''

$exitCode = 1
Push-Location $PSScriptRoot
try {
    & $pyExe @launchArgs
    $exitCode = $LASTEXITCODE
}
catch {
    Write-Host ('[ERROR] harness failed to start: ' + $_.Exception.Message) -ForegroundColor Red
    $exitCode = 1
}
Pop-Location
exit $exitCode
