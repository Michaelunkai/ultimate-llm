#Requires -Version 5.1
<#
.SYNOPSIS
    ultimate-llm hardware-aware model discovery / selection.

.CONTRACT
    powershell -File lib/discover-model.ps1 -HardwareProfileJson <path> [-FixtureKey <key>] [-Offline] [-PreferSize small|large]
      -> prints model-choice JSON to stdout, exit 0
      -> exit 2 with {"error":"no-fit"} when no catalog entry fits the hardware
      -> exit 3 with {"error":...} for bad input (missing profile / malformed JSON)

    -HardwareProfileJson may point to a raw hardware-profile JSON file OR to the fixtures
    file (lib\fixtures\profiles.json). When the fixtures file is used without -FixtureKey,
    key 'rtx5080-16gb' is selected. -FixtureKey alone loads from lib\fixtures\profiles.json.

.NOTES
    Windows PowerShell 5.1 only (no ternaries, no null-coalescing). TLS 1.2 forced before
    any HTTP call. All network calls: try/catch + -TimeoutSec 15; failures degrade silently.

    FIT RULES (frozen):
      usable_vram = vram_total_gb * 0.93 - 0.8 (min 0.5)
      Dense:  mode "vram" requires size_gb <= usable_vram.
      MoE:    "vram" if size_gb <= usable_vram; else "vram+ram" if size_gb <= usable_vram*1.6
              AND ram_gb >= 32 (score penalty 12; est_vram_gb = size_gb*0.65 because only the
              active-expert working set stays resident).
      CPU-only (vram_total_gb < 1): mode "cpu", require size_gb <= ram_gb*0.5, penalty 25.
      num_ctx: 32768 for mode "vram"; 16384 for "vram+ram" (partial residency, documented in
      reason text) and "cpu".

    SCORE = family_weight + (tool_call?4) + (coder?2) + recency_bonus(rank 0|1->3, 2->2,
    3->1, >=4->0) + (moe?2) + 1.5 if seen on ollama.com/updated - fit penalties.
    Highest score wins; tie -> alphabetical by tag.

    Catalog is extensible: lib\fixtures\catalog-override.json (array of entries with the
    same fields) is merged in; override wins by tag.
#>

param(
    [string]$HardwareProfileJson,
    [string]$FixtureKey,
    [switch]$Offline,
    [string]$PreferSize
)

# TLS 1.2 before any HTTP call.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #

function Get-EntryField {
    param($Object, [string]$Name, $Default)
    if ($null -eq $Object) { return $Default }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $Default }
    if ($null -eq $prop.Value) { return $Default }
    return $prop.Value
}

function Get-TagBase {
    param([string]$Tag)
    $idx = $Tag.IndexOf(':')
    if ($idx -ge 1) { return $Tag.Substring(0, $idx).ToLowerInvariant() }
    return $Tag.ToLowerInvariant()
}

function Get-TagParamsB {
    param([string]$Tag)
    if ($Tag -match '(?i)(\d+(?:\.\d+)?)b$') { return [double]$Matches[1] }
    return $null
}

function Merge-SourceList {
    param($Existing, [string[]]$Add)
    $seen = @{}
    $out = @()
    foreach ($s in @($Existing)) {
        $k = [string]$s
        if (-not $seen.ContainsKey($k)) { $seen[$k] = $true; $out += $k }
    }
    foreach ($s in @($Add)) {
        $k = [string]$s
        if (-not $seen.ContainsKey($k)) { $seen[$k] = $true; $out += $k }
    }
    return ,$out
}

function Get-RecencyBonus {
    param([int]$Rank)
    if ($Rank -le 1) { return 3.0 }
    if ($Rank -eq 2) { return 2.0 }
    if ($Rank -eq 3) { return 1.0 }
    return 0.0
}

# --------------------------------------------------------------------------- #
# embedded fallback catalog (mandatory minimum)                               #
# --------------------------------------------------------------------------- #

function New-EmbeddedCatalog {
    # recency_rank 1 = newest; size_gb = expected Q4_K_M download GB on ollama
    $entries = @(
        @{ tag='qwen3.5:9b';           display_name='Qwen3.5';           family='qwen3.5';     params_b=9;    quant='Q4_K_M'; size_gb=9.8;  moe=$false; tool_call=$true;  coder=$true;  family_weight=9.5; recency_rank=1 }
        @{ tag='qwen3.5:27b';          display_name='Qwen3.5';           family='qwen3.5';     params_b=27;   quant='Q4_K_M'; size_gb=17.5; moe=$false; tool_call=$true;  coder=$true;  family_weight=9.5; recency_rank=1 }
        @{ tag='qwen3.5:4b';           display_name='Qwen3.5';           family='qwen3.5';     params_b=4;    quant='Q4_K_M'; size_gb=3.2;  moe=$false; tool_call=$true;  coder=$true;  family_weight=9.5; recency_rank=1 }
        @{ tag='gpt-oss:20b';          display_name='GPT-OSS';           family='gpt-oss';     params_b=20;   quant='Q4_K_M'; size_gb=13.5; moe=$true;  tool_call=$true;  coder=$false; family_weight=9.5; recency_rank=1 }
        @{ tag='gemma4:26b-a4b-qat';   display_name='Gemma4 26B-A4B QAT'; family='gemma4';    params_b=26;   quant='QAT';    size_gb=15.0; moe=$true;  tool_call=$true;  coder=$false; family_weight=9.6; recency_rank=1 }
        @{ tag='gemma4:12b';           display_name='Gemma4';            family='gemma4';     params_b=12;   quant='Q4_K_M'; size_gb=8.0;  moe=$false; tool_call=$true;  coder=$false; family_weight=9.3; recency_rank=1 }
        @{ tag='qwen3.6:27b';          display_name='Qwen3.6';           family='qwen3.6';    params_b=27;   quant='Q4_K_M'; size_gb=18.5; moe=$false; tool_call=$true;  coder=$true;  family_weight=9.7; recency_rank=1 }
        @{ tag='gpt-oss:120b';         display_name='GPT-OSS';           family='gpt-oss';     params_b=120;  quant='Q4_K_M'; size_gb=65.0; moe=$true;  tool_call=$true;  coder=$false; family_weight=9.5; recency_rank=1 }
        @{ tag='qwen3-coder:30b';      display_name='Qwen3-Coder';       family='qwen3-coder'; params_b=30;   quant='Q4_K_M'; size_gb=19.0; moe=$true;  tool_call=$true;  coder=$true;  family_weight=9.0; recency_rank=2 }
        @{ tag='qwen3:30b-a3b';        display_name='Qwen3';             family='qwen3';       params_b=30;   quant='Q4_K_M'; size_gb=19.0; moe=$true;  tool_call=$true;  coder=$false; family_weight=8.5; recency_rank=2 }
        @{ tag='glm-4.5-air:27b';      display_name='GLM-4.5-Air';       family='glm';         params_b=27;   quant='Q4_K_M'; size_gb=17.0; moe=$true;  tool_call=$true;  coder=$false; family_weight=9.0; recency_rank=2 }
        @{ tag='deepseek-r1:14b';      display_name='DeepSeek-R1';       family='deepseek';    params_b=14;   quant='Q4_K_M'; size_gb=9.0;  moe=$false; tool_call=$false; coder=$false; family_weight=7.0; recency_rank=3 }
        @{ tag='deepseek-r1:32b';      display_name='DeepSeek-R1';       family='deepseek';    params_b=32;   quant='Q4_K_M'; size_gb=20.0; moe=$false; tool_call=$false; coder=$false; family_weight=7.5; recency_rank=3 }
        @{ tag='devstral:24b';         display_name='Devstral';          family='devstral';    params_b=24;   quant='Q4_K_M'; size_gb=14.0; moe=$false; tool_call=$true;  coder=$true;  family_weight=8.0; recency_rank=3 }
        @{ tag='mistral-small3.2:24b'; display_name='Mistral Small 3.2'; family='mistral';     params_b=24;   quant='Q4_K_M'; size_gb=14.0; moe=$false; tool_call=$true;  coder=$false; family_weight=8.0; recency_rank=3 }
        @{ tag='llama4:scout';         display_name='Llama 4 Scout';     family='llama4';      params_b=109;  quant='Q4_K_M'; size_gb=67.0; moe=$true;  tool_call=$true;  coder=$false; family_weight=8.5; recency_rank=2 }
        @{ tag='phi-4:14b';            display_name='Phi-4';             family='phi';         params_b=14;   quant='Q4_K_M'; size_gb=9.0;  moe=$false; tool_call=$true;  coder=$false; family_weight=7.0; recency_rank=4 }
        @{ tag='phi-4-mini';           display_name='Phi-4-mini';        family='phi';         params_b=3.8;  quant='Q4_K_M'; size_gb=2.5;  moe=$false; tool_call=$true;  coder=$false; family_weight=7.0; recency_rank=4 }
        @{ tag='gemma3:12b';           display_name='Gemma 3';           family='gemma3';      params_b=12;   quant='Q4_K_M'; size_gb=8.0;  moe=$false; tool_call=$false; coder=$false; family_weight=6.5; recency_rank=3 }
        @{ tag='gemma3:27b';           display_name='Gemma 3';           family='gemma3';      params_b=27;   quant='Q4_K_M'; size_gb=17.0; moe=$false; tool_call=$false; coder=$false; family_weight=7.0; recency_rank=3 }
        @{ tag='gemma3:4b';            display_name='Gemma 3';           family='gemma3';      params_b=4;    quant='Q4_K_M'; size_gb=3.3;  moe=$false; tool_call=$false; coder=$false; family_weight=6.5; recency_rank=3 }
        # 2026-10 scout refresh (sources: ollama.com/library, qwen.ai -- see
        # docs/IMPROVEMENTS-2026-10.md). Entries above the vram/vram+ram fit lines are
        # inert on this 16GB machine by design - they exist for future hardware.
        @{ tag='qwen3.6:35b';          display_name='Qwen3.6';            family='qwen3.6';     params_b=35;   quant='Q4_K_M'; size_gb=23.0; moe=$true;  tool_call=$true;  coder=$true;  family_weight=9.7; recency_rank=0 }
        @{ tag='qwen3.8:27b';          display_name='Qwen3.8';            family='qwen3.8';     params_b=27;   quant='Q4_K_M'; size_gb=18.0; moe=$false; tool_call=$true;  coder=$true;  family_weight=9.4; recency_rank=0 }
        @{ tag='glm-4.7-flash';        display_name='GLM-4.7-Flash';      family='glm';         params_b=30;   quant='q4_K_M'; size_gb=19.0; moe=$true;  tool_call=$true;  coder=$true;  family_weight=9.2; recency_rank=1 }
        @{ tag='laguna-xs.2';          display_name='Laguna XS.2';        family='laguna';      params_b=33;   quant='q4_K_M'; size_gb=23.0; moe=$true;  tool_call=$true;  coder=$true;  family_weight=9.2; recency_rank=1 }
        @{ tag='devstral-small-2:24b'; display_name='Devstral Small 2';   family='devstral';    params_b=24;   quant='Q4_K_M'; size_gb=15.0; moe=$false; tool_call=$true;  coder=$true;  family_weight=9.1; recency_rank=2 }
    )
    $out = @()
    foreach ($e in $entries) {
        $e['sources'] = @('embedded-catalog')
        $e['fresh'] = $false
        $out += $e
    }
    return ,$out
}

function Add-CatalogOverride {
    param([hashtable[]]$Catalog)
    $byTag = @{}
    foreach ($e in $Catalog) { $byTag[[string]$e['tag']] = $e }
    $overrideUsed = $false
    $overridePath = Join-Path $PSScriptRoot 'fixtures\catalog-override.json'
    if (Test-Path -LiteralPath $overridePath) {
        try {
            $raw = Get-Content -LiteralPath $overridePath -Raw -ErrorAction Stop | ConvertFrom-Json
            foreach ($item in @($raw)) {
                if ($null -eq $item) { continue }
                $tag = Get-EntryField $item 'tag' $null
                if ([string]::IsNullOrWhiteSpace([string]$tag)) { continue }
                $tag = [string]$tag
                $paramsB = Get-EntryField $item 'params_b' (Get-TagParamsB $tag)
                $sizeGb = [double](Get-EntryField $item 'size_gb' 0)
                if ($sizeGb -le 0) {
                    if ($null -ne $paramsB -and [double]$paramsB -gt 0) {
                        $sizeGb = [math]::Round(0.75 * [double]$paramsB, 2)
                    } else {
                        continue
                    }
                }
                $entry = @{
                    tag           = $tag
                    display_name  = [string](Get-EntryField $item 'display_name' $tag)
                    family        = [string](Get-EntryField $item 'family' (Get-TagBase $tag))
                    params_b      = $paramsB
                    quant         = [string](Get-EntryField $item 'quant' 'Q4_K_M')
                    size_gb       = [double]$sizeGb
                    moe           = [bool](Get-EntryField $item 'moe' $false)
                    tool_call     = [bool](Get-EntryField $item 'tool_call' $false)
                    coder         = [bool](Get-EntryField $item 'coder' $false)
                    family_weight = [double](Get-EntryField $item 'family_weight' 6.0)
                    recency_rank  = [int](Get-EntryField $item 'recency_rank' 0)
                    fresh         = $false
                    sources       = @('catalog-override')
                }
                if ($byTag.ContainsKey($tag)) {
                    $entry['sources'] = Merge-SourceList -Existing $byTag[$tag]['sources'] -Add @('catalog-override')
                }
                $byTag[$tag] = $entry   # override wins by tag
                $overrideUsed = $true
            }
        } catch { }
    }
    return @{ catalog = @($byTag.Values); override_used = $overrideUsed }
}

# --------------------------------------------------------------------------- #
# online pipeline (all failures degrade silently)                             #
# --------------------------------------------------------------------------- #

function Get-ModelsDevEntries {
    param([hashtable]$KnownBases)
    $out = @()
    $seenTags = @{}
    try {
        $api = Invoke-RestMethod -Uri 'https://models.dev/api.json' -TimeoutSec 15 -ErrorAction Stop
        if ($null -eq $api) { return ,$out }
        foreach ($provProp in @($api.PSObject.Properties)) {
            $provId = [string]$provProp.Name
            $prov = $provProp.Value
            if ($null -eq $prov) { continue }
            $modelsVal = Get-EntryField $prov 'models' $null
            if ($null -eq $modelsVal) { continue }
            $isOllamaProvider = ($provId.ToLowerInvariant().Contains('ollama'))
            foreach ($mProp in @($modelsVal.PSObject.Properties)) {
                try {
                    $m = $mProp.Value
                    if ($null -eq $m) { continue }
                    if (-not [bool](Get-EntryField $m 'tool_call' $false)) { continue }

                    $id = $null
                    foreach ($cand in @('id', 'model', 'name')) {
                        $v = Get-EntryField $m $cand $null
                        if ($null -ne $v -and -not [string]::IsNullOrWhiteSpace([string]$v)) { $id = [string]$v; break }
                    }
                    if ([string]::IsNullOrWhiteSpace($id)) { continue }
                    if ($id.Contains('/')) { $id = $id.Substring($id.LastIndexOf('/') + 1) }
                    $base = Get-TagBase $id
                    # keep ollama-ish ids only: ollama provider, tag-form id, or known base family
                    if (-not $isOllamaProvider -and -not $id.Contains(':') -and -not $KnownBases.ContainsKey($base)) { continue }
                    if ($seenTags.ContainsKey($id)) { continue }
                    $seenTags[$id] = $true

                    # params (number or "20B"-style string)
                    $paramsB = $null
                    $pv = Get-EntryField $m 'params_b' $null
                    if ($null -eq $pv) { $pv = Get-EntryField $m 'parameters' $null }
                    if ($null -ne $pv) {
                        if ($pv -is [string]) {
                            if ($pv -match '([0-9]+(?:\.[0-9]+)?)') { $paramsB = [double]$Matches[1] }
                        } else {
                            try { $paramsB = [double]$pv } catch { $paramsB = $null }
                        }
                    }

                    # size ("13.5 GB" style string or number); default 0.75*params_b GB
                    $sizeGb = $null
                    $sv = Get-EntryField $m 'size' $null
                    if ($null -ne $sv) {
                        if ($sv -is [string]) {
                            if ($sv -match '([0-9]+(?:\.[0-9]+)?)\s*(GB|MB)') {
                                $sizeGb = [double]$Matches[1]
                                if ($Matches[2].ToUpperInvariant() -eq 'MB') { $sizeGb = [math]::Round($sizeGb / 1024.0, 2) }
                            }
                        } else {
                            try { $sizeGb = [double]$sv } catch { $sizeGb = $null }
                        }
                    }
                    if (($null -eq $sizeGb) -or ($sizeGb -le 0)) {
                        if ($null -ne $paramsB -and $paramsB -gt 0) {
                            $sizeGb = [math]::Round(0.75 * $paramsB, 2)
                        } else {
                            continue
                        }
                    }

                    $name = [string](Get-EntryField $m 'name' $id)
                    $out += @{
                        tag           = $id
                        display_name  = $name
                        family        = $base
                        params_b      = $paramsB
                        quant         = 'Q4_K_M'
                        size_gb       = [double]$sizeGb
                        moe           = [bool](Get-EntryField $m 'moe' $false)
                        tool_call     = $true
                        coder         = (($id + ' ' + $name) -match '(?i)coder|code')
                        family_weight = 6.0
                        recency_rank  = 0
                        sources       = @('models.dev')
                        fresh         = $false
                    }
                } catch { continue }
            }
        }
    } catch { }
    return ,$out
}

function Get-OllamaComSignals {
    # existence confirmation (library page) + freshness signal (updated page) + size tokens
    $signals = @{
        library = New-Object 'System.Collections.Generic.HashSet[string]'
        updated = New-Object 'System.Collections.Generic.HashSet[string]'
        sizes   = @{}
        ok      = $false
    }
    $targets = @(
        @('https://ollama.com/library', 'library'),
        @('https://ollama.com/search?v=updated', 'updated')
    )
    foreach ($t in $targets) {
        try {
            $resp = Invoke-WebRequest -Uri $t[0] -TimeoutSec 15 -UseBasicParsing -ErrorAction Stop
            if ($null -eq $resp) { continue }
            $content = [string]$resp.Content
            if ([string]::IsNullOrWhiteSpace($content)) { continue }
            $signals['ok'] = $true

            $names = @()
            foreach ($mm in [regex]::Matches($content, 'href="/library/([a-z0-9.\-_]+)"')) {
                $names += ,@($mm.Index, $mm.Groups[1].Value.ToLowerInvariant())
            }
            foreach ($n in $names) {
                [void]$signals['library'].Add($n[1])
                if ($t[1] -eq 'updated') { [void]$signals['updated'].Add($n[1]) }
            }

            # attach nearby size tokens to the nearest preceding library name
            foreach ($sm in [regex]::Matches($content, '([0-9]+(?:\.[0-9]+)?)\s*(GB|MB)')) {
                $owner = $null
                for ($j = $names.Count - 1; $j -ge 0; $j--) {
                    if ($names[$j][0] -lt $sm.Index) { $owner = $names[$j][1]; break }
                }
                if ($null -ne $owner -and -not $signals['sizes'].ContainsKey($owner)) {
                    $gb = [double]$sm.Groups[1].Value
                    if ($sm.Groups[2].Value.ToUpperInvariant() -eq 'MB') { $gb = [math]::Round($gb / 1024.0, 2) }
                    $signals['sizes'][$owner] = $gb
                }
            }
        } catch { }
    }
    return $signals
}

# --------------------------------------------------------------------------- #
# fit + scoring                                                               #
# --------------------------------------------------------------------------- #

function Get-FitInfo {
    param($Entry, [double]$UsableVram, [double]$Ram, [bool]$CpuOnly)
    $size = [double]$Entry['size_gb']
    if ($CpuOnly) {
        if ($size -le ($Ram * 0.5)) {
            return @{ ok = $true; mode = 'cpu'; penalty = 25.0; est_vram_gb = 0.0 }
        }
        return @{ ok = $false; mode = ''; penalty = 0.0; est_vram_gb = 0.0 }
    }
    if ($size -le $UsableVram) {
        return @{ ok = $true; mode = 'vram'; penalty = 0.0; est_vram_gb = $size }
    }
    if ([bool]$Entry['moe'] -and ($size -le ($UsableVram * 1.6)) -and ($Ram -ge 32)) {
        return @{ ok = $true; mode = 'vram+ram'; penalty = 12.0; est_vram_gb = [math]::Round($size * 0.65, 2) }
    }
    return @{ ok = $false; mode = ''; penalty = 0.0; est_vram_gb = 0.0 }
}

function New-ReasonText {
    param($Entry, [string]$Mode, [double]$EstVramGb, [double]$UsableVram, [double]$VramTotal, [double]$Ram, [bool]$Fresh)
    $parts = @()
    $sizeTxt = [math]::Round([double]$Entry['size_gb'], 1)
    if ($Mode -eq 'vram') {
        $parts += ('fits ' + [math]::Round($VramTotal, 0) + 'GB VRAM fully (size ' + $sizeTxt + 'GB <= usable ' + [math]::Round($UsableVram, 1) + 'GB)')
    } elseif ($Mode -eq 'vram+ram') {
        $parts += ('MoE offload to RAM: size ' + $sizeTxt + 'GB > usable VRAM ' + [math]::Round($UsableVram, 1) + 'GB; active-expert working set ~' + $EstVramGb + 'GB resident (0.65x); ctx reduced to 16384')
    } else {
        $parts += ('CPU-only: size ' + $sizeTxt + 'GB fits 50% of ' + [math]::Round($Ram, 0) + 'GB RAM')
    }
    if ([bool]$Entry['moe']) { $parts += 'MoE fast' }
    if ([bool]$Entry['tool_call']) { $parts += 'native tool calling' }
    if ([bool]$Entry['coder']) { $parts += 'code-tuned' }
    if ($Fresh) {
        $parts += 'freshness +1.5 (seen on ollama.com/updated)'
    } else {
        $parts += ('rank-' + [int]$Entry['recency_rank'] + ' freshness')
    }
    return ($parts -join '; ')
}

# --------------------------------------------------------------------------- #
# frozen interface                                                            #
# --------------------------------------------------------------------------- #

function Select-UltimateModel {
    param(
        [object]$HardwareProfile,
        [switch]$Offline,
        [string]$PreferSize
    )

    if ($null -eq $HardwareProfile) { return $null }

    # --- hardware -----------------------------------------------------------
    $vramTotal = [double](Get-EntryField $HardwareProfile 'vram_total_gb' -1.0)
    if ($vramTotal -lt 0) {
        $vramTotal = 0.0
        foreach ($g in @(Get-EntryField $HardwareProfile 'gpus' @())) {
            if ($null -ne $g) { $vramTotal = $vramTotal + [double](Get-EntryField $g 'vram_gb' 0) }
        }
    }
    $ram = [double](Get-EntryField $HardwareProfile 'ram_gb' 8)
    $cpuOnly = ($vramTotal -lt 1)
    $usableVram = ($vramTotal * 0.93) - 0.8
    if ($usableVram -lt 0.5) { $usableVram = 0.5 }

    # --- catalog: embedded + optional override ------------------------------
    $built = Add-CatalogOverride -Catalog (New-EmbeddedCatalog)
    $catalog = @($built.catalog)
    $byTag = @{}
    $byBase = @{}
    foreach ($e in $catalog) {
        $byTag[[string]$e['tag']] = $e
        $b = Get-TagBase $e['tag']
        if (-not $byBase.ContainsKey($b)) { $byBase[$b] = $e }
    }

    # --- online pipeline ----------------------------------------------------
    $onlineUsed = $false
    if (-not $Offline) {
        $online = @(Get-ModelsDevEntries -KnownBases $byBase)
        if ($online.Count -gt 0) {
            $onlineUsed = $true
            foreach ($oe in $online) {
                $t = [string]$oe['tag']
                $b = Get-TagBase $t
                if ($byTag.ContainsKey($t)) {
                    $ex = $byTag[$t]
                    $ex['recency_rank'] = 0
                    $ex['sources'] = Merge-SourceList -Existing $ex['sources'] -Add @('models.dev')
                } else {
                    if ($byBase.ContainsKey($b)) {
                        $ref = $byBase[$b]
                        $oe['family'] = [string]$ref['family']
                        $oe['display_name'] = [string]$ref['display_name']
                        $oe['family_weight'] = [double]$ref['family_weight']
                    }
                    $catalog = $catalog + $oe
                    $byTag[$t] = $oe
                    if (-not $byBase.ContainsKey($b)) { $byBase[$b] = $oe }
                }
            }
        }

        $signals = Get-OllamaComSignals
        $confirmed = 0
        foreach ($e in $catalog) {
            $b = Get-TagBase $e['tag']
            if ($signals['library'].Contains($b)) {
                $confirmed = $confirmed + 1
                $e['sources'] = Merge-SourceList -Existing $e['sources'] -Add @('ollama.com')
                if ($signals['updated'].Contains($b)) { $e['fresh'] = $true }
            }
        }
        if ($signals['ok'] -and ($confirmed -gt 0)) { $onlineUsed = $true }
    }

    # --- score + fit --------------------------------------------------------
    $scored = New-Object System.Collections.Generic.List[object]
    foreach ($e in $catalog) {
        $fit = Get-FitInfo -Entry $e -UsableVram $usableVram -Ram $ram -CpuOnly $cpuOnly
        if (-not $fit['ok']) { continue }

        $score = [double]$e['family_weight']
        if ([bool]$e['tool_call']) { $score = $score + 4.0 }
        if ([bool]$e['coder']) { $score = $score + 2.0 }
        $score = $score + (Get-RecencyBonus -Rank ([int]$e['recency_rank']))
        if ([bool]$e['moe']) { $score = $score + 2.0 }
        if ([bool]$e['fresh']) { $score = $score + 1.5 }
        $score = $score - [double]$fit['penalty']

        if ($PreferSize -eq 'small') {
            $score = $score + [math]::Max(0.0, 4.0 - [double]$e['size_gb'])
        } elseif ($PreferSize -eq 'large') {
            $score = $score + [math]::Min(4.0, ([double]$e['size_gb'] / 8.0))
        }

        $numCtx = 16384
        if ($fit['mode'] -eq 'vram') { $numCtx = 32768 }

        $reasonText = New-ReasonText -Entry $e -Mode $fit['mode'] -EstVramGb ([double]$fit['est_vram_gb']) -UsableVram $usableVram -VramTotal $vramTotal -Ram $ram -Fresh ([bool]$e['fresh'])

        $scored.Add([pscustomobject]@{
            tag          = [string]$e['tag']
            display_name = [string]$e['display_name']
            family       = [string]$e['family']
            params_b     = $e['params_b']
            quant        = [string]$e['quant']
            size_gb      = [double]$e['size_gb']
            moe          = [bool]$e['moe']
            tool_call    = [bool]$e['tool_call']
            coder        = [bool]$e['coder']
            score        = [math]::Round($score, 2)
            reason       = $reasonText
            sources      = (Merge-SourceList -Existing $e['sources'] -Add @())
            mode         = [string]$fit['mode']
            est_vram_gb  = [double]([math]::Round([double]$fit['est_vram_gb'], 2))
            num_ctx      = $numCtx
        })
    }

    if ($scored.Count -eq 0) { return $null }

    $winner = $scored |
        Sort-Object -Property @{Expression = 'score'; Descending = $true}, @{Expression = 'tag'; Descending = $false} |
        Select-Object -First 1

    $result = [ordered]@{
        model         = [string]$winner.tag
        display_name  = [string]$winner.display_name
        family        = [string]$winner.family
        params_b      = $winner.params_b
        quant         = [string]$winner.quant
        size_gb       = [double]$winner.size_gb
        moe           = [bool]$winner.moe
        tool_call     = [bool]$winner.tool_call
        coder         = [bool]$winner.coder
        score         = [double]$winner.score
        reason        = [string]$winner.reason
        sources       = @($winner.sources)
        fit           = [ordered]@{ mode = [string]$winner.mode; est_vram_gb = [double]$winner.est_vram_gb }
        runner        = [ordered]@{ num_ctx = [int]$winner.num_ctx }
        discovered_at = (Get-Date).ToUniversalTime().ToString('o')
        online_used   = [bool]$onlineUsed
    }
    return $result
}

# --------------------------------------------------------------------------- #
# profile resolution                                                          #
# --------------------------------------------------------------------------- #

function Resolve-HardwareProfile {
    param([string]$HardwareProfileJsonPath, [string]$FixtureKeyName)
    $fixturesFile = Join-Path $PSScriptRoot 'fixtures\profiles.json'

    if (-not [string]::IsNullOrWhiteSpace($FixtureKeyName) -and (Test-Path -LiteralPath $fixturesFile)) {
        try {
            $fx = Get-Content -LiteralPath $fixturesFile -Raw -ErrorAction Stop | ConvertFrom-Json
            $found = $fx.profiles.PSObject.Properties[$FixtureKeyName]
            if ($null -ne $found) { return $found.Value }
        } catch { }
    }

    if ([string]::IsNullOrWhiteSpace($HardwareProfileJsonPath)) { return $null }
    if (-not (Test-Path -LiteralPath $HardwareProfileJsonPath)) { return $null }

    try {
        $doc = Get-Content -LiteralPath $HardwareProfileJsonPath -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($null -eq $doc) { return $null }
        if ($null -ne (Get-EntryField $doc 'profiles' $null)) {
            # fixtures file: -FixtureKey selects; default key rtx5080-16gb
            $key = $FixtureKeyName
            if ([string]::IsNullOrWhiteSpace($key)) { $key = 'rtx5080-16gb' }
            $found = $doc.profiles.PSObject.Properties[$key]
            if ($null -ne $found) { return $found.Value }
            return $null
        }
        return $doc
    } catch {
        return $null
    }
}

# --------------------------------------------------------------------------- #
# background better-model check (fast-launch sidecar writer)                  #
# --------------------------------------------------------------------------- #

function Write-ModelCheck {
    # Runs inside the detached fast-launch child: re-runs ONLINE discovery with the
    # hardware cached in .bootstrap-state.json and writes the verdict to the sidecar
    # (.model-check.json). NEVER writes .bootstrap-state.json (single-writer rule:
    # only the full path and /update touch the state file).
    param([string]$StateFile, [string]$OutFile)

    $result = [ordered]@{
        status        = 'error'
        current       = $null
        current_score = 0.0
        best          = $null
        score_delta   = 0.0
        size_gb       = 0.0
        num_ctx       = 32768
        reason        = ''
        checked_at    = (Get-Date).ToUniversalTime().ToString('o')
    }
    try {
        if ([string]::IsNullOrWhiteSpace($StateFile) -or -not (Test-Path -LiteralPath $StateFile)) {
            $result.reason = 'no-state-file'
        }
        else {
            $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
            $current = [string]$state.model
            $result.current = $current
            $result.current_score = [double](Get-EntryField $state 'model_score' 0.0)
            $hw = $state.hw
            if ([string]::IsNullOrWhiteSpace($current)) {
                $result.reason = 'state-has-no-model'
            }
            elseif ($null -eq $hw) {
                $result.reason = 'state-has-no-hw'
            }
            elseif ($result.current_score -le 0.0) {
                # Legacy state written before model_score existed: never claim a
                # phantom upgrade. One -Full run stores model_score and enables
                # upgrade detection from then on.
                $result.status = 'ok'
                $result.reason = 'state lacks model_score - run once with -Full to enable upgrade checks'
            }
            else {
                # NB: Get-EntryField reads PSObject.Properties -- hashtables are
                # invisible to it, so the profile must be a pscustomobject.
                $profile = [pscustomobject]@{
                    vram_total_gb = [double]$hw.vram_gb
                    ram_gb        = [double]$hw.ram_gb
                    gpus          = @([pscustomobject]@{ vram_gb = [double]$hw.vram_gb })
                }
                $choice = Select-UltimateModel -HardwareProfile $profile
                if ($null -eq $choice) {
                    $result.status = 'error'
                    $result.reason = 'discovery returned no fit (offline?)'
                }
                else {
                    $result.best = [string]$choice.model
                    $result.size_gb = [double]$choice.size_gb
                    $result.num_ctx = [int]$choice.runner.num_ctx
                    $result.reason = [string]$choice.reason
                    $delta = [math]::Round(([double]$choice.score - $result.current_score), 2)
                    $result.score_delta = $delta
                    if ($choice.model -eq $current) {
                        $result.status = 'ok'
                        $result.reason = 'current model is still the best fit (score ' + [double]$choice.score + ')'
                    }
                    elseif (($delta -ge 2.0) -and ([string]$choice.fit.mode -eq 'vram')) {
                        $result.status = 'better'
                    }
                    else {
                        $result.status = 'ok'
                        $result.reason = 'best alternative not clearly better (delta ' + $delta + ', mode ' + [string]$choice.fit.mode + ')'
                    }
                }
            }
        }
    }
    catch {
        $result.status = 'error'
        $result.reason = $_.Exception.Message
    }
    finally {
        try {
            $tmp = $OutFile + '.tmp'
            (ConvertTo-Json -InputObject $result -Depth 4 -Compress) | Set-Content -LiteralPath $tmp -Encoding ASCII
            Move-Item -LiteralPath $tmp -Destination $OutFile -Force
        } catch { }
    }
    return $result
}

# --------------------------------------------------------------------------- #
# entry point (silent when dot-sourced)                                       #
# --------------------------------------------------------------------------- #

if ($MyInvocation.InvocationName -ne '.') {
    $hw = Resolve-HardwareProfile -HardwareProfileJsonPath $HardwareProfileJson -FixtureKeyName $FixtureKey
    if ($null -eq $hw) {
        $errObj = [ordered]@{ error = 'profile-not-found'; fixture_key = $FixtureKey; hardware_profile_json = $HardwareProfileJson }
        Write-Output (ConvertTo-Json -InputObject $errObj -Depth 6)
        exit 3
    }

    $choice = $null
    try {
        $choice = Select-UltimateModel -HardwareProfile $hw -Offline:$Offline -PreferSize $PreferSize
    } catch {
        $errObj = [ordered]@{ error = 'internal-error'; detail = $_.Exception.Message }
        Write-Output (ConvertTo-Json -InputObject $errObj -Depth 6)
        exit 3
    }

    if ($null -eq $choice) {
        Write-Output (ConvertTo-Json -InputObject ([ordered]@{ error = 'no-fit' }) -Depth 6)
        exit 2
    }

    Write-Output (ConvertTo-Json -InputObject $choice -Depth 6)
    exit 0
}
