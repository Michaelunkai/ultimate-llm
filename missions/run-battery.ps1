# run-battery.ps1 -- certified sequential mission battery (D9).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File run-battery.ps1 -Label S1
# One PID lockfile prevents the concurrency incident class (two loops corrupt sandboxes).
# Per mission: prewarm gate -> wipe sandbox -> outer-repo git guard -> run harness
# mission -> verify.py -> log. Sweep PASS = 12/12 with harness exit 0 and no repo drift.

param([Parameter(Mandatory = $true)][string]$Label)

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$lock = Join-Path $root '.battery.lock'
if (Test-Path -LiteralPath $lock) {
    $old = (Get-Content -LiteralPath $lock -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($old -match '^\d+$') {
        $alive = Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue
        if ($alive) {
            Write-Output "[LOCK] battery loop already running (pid $old) - refusing to start."
            exit 1
        }
    }
}
"$PID" | Set-Content -LiteralPath $lock -Encoding ascii

$venvPy   = Join-Path (Split-Path $root -Parent) '.venv\Scripts\python.exe'
$harness  = Join-Path (Split-Path $root -Parent) 'harness\harness.py'
$verifier = Join-Path $root 'verify.py'
$outerGit = Split-Path (Split-Path $root -Parent) -Parent
$batteryLog = Join-Path $root ("logs\battery-$Label.txt")
"=== battery $Label started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Set-Content -LiteralPath $batteryLog -Encoding utf8

$pass = 0; $failList = @()
try {
    foreach ($i in 1..12) {
        $name = 'M{0:D2}' -f $i
        $sandbox = Join-Path $root $name
        $mlog = Join-Path $root ("logs\{0}-{1}.log" -f $name, $Label)

        # 1) prewarm gate: daemon must answer a 1-token chat (max 120s).
        #    keep_alive 24h matches the harness - the Ollama default (5m) can
        #    let the model UNLOAD mid-mission and stall in-flight streams
        #    (proven: battery ctx-endurance2, M01 died at the expiry boundary).
        $warm = $false
        $body = '{"model":"gpt-oss:20b","stream":false,"messages":[{"role":"user","content":"ping"}],"options":{"num_predict":1},"keep_alive":"24h"}'
        foreach ($a in 1..40) {
            try {
                Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:11434/api/chat' -ContentType 'application/json' -TimeoutSec 120 -Body $body | Out-Null
                $warm = $true; break
            } catch { Start-Sleep -Seconds 3 }
        }
        if (-not $warm) {
            Write-Output "[FAIL] $name -- daemon did not answer the prewarm gate" | Add-Content -LiteralPath $batteryLog -Encoding utf8
            $failList += "$name(prewarm)"; continue
        }

        # 2) outer-repo guard snapshot
        $branchBefore = (git -C $outerGit branch --show-current 2>$null)
        $shaBefore    = (git -C $outerGit rev-parse HEAD 2>$null)

        # 3) clean sandbox
        if (Test-Path -LiteralPath $sandbox) { Remove-Item -Recurse -Force -LiteralPath $sandbox }

        # 4) run the mission (one scripted retry on fail - both attempts logged)
        & $venvPy $harness --model gpt-oss:20b --num-ctx 32768 --yes --mission ("@{0}.txt" -f (Join-Path $root $name)) *> $mlog
        $hexit = $LASTEXITCODE
        $vexit = 1
        $guardOk = $false
        $attempt = 1
        while ($attempt -le 2) {
            # 5) outer-repo guard check
            $shaAfter    = (git -C $outerGit rev-parse HEAD 2>$null)
            $branchAfter = (git -C $outerGit branch --show-current 2>$null)
            $guardOk = ($shaAfter -eq $shaBefore -and $branchAfter -eq $branchBefore)

            # 6) independent verification
            $vout = & $venvPy $verifier $name 2>&1
            $vexit = $LASTEXITCODE
            ($vout | Out-String) | Add-Content -LiteralPath $mlog -Encoding utf8

            if ($hexit -eq 0 -and $vexit -eq 0 -and $guardOk) { break }

            if ($attempt -eq 1) {
                Write-Output "[RETRY] $name attempt 1 failed (harness=$hexit verify=$vexit guard=$guardOk) - one scripted retry"
                if (Test-Path -LiteralPath $sandbox) { Remove-Item -Recurse -Force -LiteralPath $sandbox }
                $mlog = Join-Path $root ("logs\{0}-{1}-retry.log" -f $name, $Label)
                & $venvPy $harness --model gpt-oss:20b --num-ctx 32768 --yes --mission ("@{0}.txt" -f (Join-Path $root $name)) *> $mlog
                $hexit = $LASTEXITCODE
                $attempt = 2
            } else { break }
        }

        $verdict = 'FAIL'
        if ($hexit -eq 0 -and $vexit -eq 0 -and $guardOk) { $verdict = 'PASS'; $pass++ } else { $failList += $name }
        if (-not $guardOk) {
            $drift = "GIT GUARD: outer repo drifted ($branchBefore/$shaBefore -> $branchAfter/$shaAfter)"
            Write-Output $drift | Add-Content -LiteralPath $batteryLog -Encoding utf8
            Write-Output $drift | Add-Content -LiteralPath $mlog -Encoding utf8
        }
        $line = "[{0}] {1}  harness_exit={2} verify_exit={3} guard={4}  ({5})" -f $verdict, $name, $hexit, $vexit, $guardOk, (Get-Date -Format 'HH:mm:ss')
        Write-Output $line
        Write-Output $line | Add-Content -LiteralPath $batteryLog -Encoding utf8
    }
} finally {
    Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
}

$summary = "RESULT: {0}/12 passed; failures: [{1}]" -f $pass, ($failList -join ',')
Write-Output $summary
Write-Output $summary | Add-Content -LiteralPath $batteryLog -Encoding utf8
Write-Output "log: $batteryLog"
if ($pass -eq 12) { exit 0 } else { exit 1 }
