# ============================================================
# rerun_s6.ps1
#
# S6_NOISY_BASELINE - control run, NO fault injected. Measures the
# false-positive rate of the pipeline under genuinely normal
# operating conditions (real load-generator traffic, no injected
# fault of any kind).
#
# expected_root_cause_service: "none", expected_fault_class: "none"
# -- every ANOMALY_CONFIRMED verdict logged in this scenario is, by
# definition, a false positive. This is the direct counterpart to
# S1-S5's detection-rate numbers: without a real S6 control, you
# cannot report a meaningful false-positive rate (FPR) alongside
# your true-positive detection rates.
#
# 12 CYCLES, 5 RUNS: matching S1/S2/S4/S5's convention for
# consistency, though since nothing is injected, cycle count mostly
# just determines how much "normal operation" time you observe.
#
# USAGE:
#   .\rerun_s6.ps1
#   .\rerun_s6.ps1 -StartFromRun 3
# ============================================================

param(
    [int]$StartFromRun = 1
)

$ErrorActionPreference = "Continue"
$NAMESPACE = "microservices-demo"
$MONITORING_NS = "monitoring"
$CYCLES = 12
$INTERVAL = 30

$Global:PortForwardJobs = @()

function Start-PortForwards {
    Write-Host "[Setup] Starting port-forwards (Prometheus, Jaeger, Loki)..." -ForegroundColor Cyan
    $Global:PortForwardJobs += Start-Job -ScriptBlock {
        kubectl port-forward -n $using:MONITORING_NS svc/monitoring-kube-prometheus-prometheus 9090:9090
    }
    $Global:PortForwardJobs += Start-Job -ScriptBlock {
        kubectl port-forward -n $using:MONITORING_NS svc/jaeger 16686:16686
    }
    $Global:PortForwardJobs += Start-Job -ScriptBlock {
        kubectl port-forward -n $using:MONITORING_NS svc/loki-stack 3100:3100
    }
    Write-Host "[Setup] Waiting for port-forwards to establish..." -ForegroundColor Cyan
    Start-Sleep -Seconds 6
    try {
        Invoke-WebRequest -Uri "http://localhost:9090/-/healthy" -TimeoutSec 5 -UseBasicParsing | Out-Null
        Write-Host "  [OK] Prometheus reachable" -ForegroundColor Green
    } catch { Write-Host "  [WARN] Prometheus not ready: $_" -ForegroundColor Yellow }
    try {
        Invoke-WebRequest -Uri "http://localhost:16686" -TimeoutSec 5 -UseBasicParsing | Out-Null
        Write-Host "  [OK] Jaeger reachable" -ForegroundColor Green
    } catch { Write-Host "  [WARN] Jaeger not ready: $_" -ForegroundColor Yellow }
    try {
        Invoke-WebRequest -Uri "http://localhost:3100/ready" -TimeoutSec 5 -UseBasicParsing | Out-Null
        Write-Host "  [OK] Loki reachable" -ForegroundColor Green
    } catch { Write-Host "  [WARN] Loki not ready: $_" -ForegroundColor Yellow }
}

function Stop-PortForwards {
    Write-Host "[Cleanup] Stopping port-forwards..." -ForegroundColor DarkGray
    foreach ($job in $Global:PortForwardJobs) {
        Stop-Job -Job $job -ErrorAction SilentlyContinue
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    }
    $Global:PortForwardJobs = @()
}

function Inject-S6-NoisyBaseline {
    Write-Host "[Fault] S6: No fault injected (control run)" -ForegroundColor Magenta
    # Nothing to inject. Return $true so the main loop's
    # injection-verification pattern (consistent with every other
    # scenario script) still applies cleanly -- there's simply
    # nothing to verify here.
    return $true
}

function Cleanup-S6-NoisyBaseline {
    # Nothing to clean up -- no fault was ever injected.
    Write-Host "[Cleanup] S6: Nothing to clean up (control run)" -ForegroundColor DarkGray
}

function Wait-ForStableBaseline {
    Write-Host "[Baseline] Verifying cluster is stable before next run..." -ForegroundColor DarkCyan
    $stableCount = 0
    $maxWaitCycles = 10
    $cycle = 0
    while ($stableCount -lt 2 -and $cycle -lt $maxWaitCycles) {
        $cycle++
        Start-Sleep -Seconds 15
        $notReady = kubectl get pods -n $NAMESPACE --field-selector=status.phase!=Running -o name 2>$null
        if (-not $notReady) {
            $stableCount++
            Write-Host "  Stable check $stableCount/2 passed"
        } else {
            $stableCount = 0
            Write-Host "  Not stable yet, retrying... ($notReady)"
        }
    }
    if ($stableCount -lt 2) {
        Write-Host "  [WARN] Baseline stability timeout - proceeding anyway" -ForegroundColor Yellow
    } else {
        Write-Host "  [OK] Baseline confirmed stable" -ForegroundColor Green
    }
}

# ============================================================
# Backup old S6 data before overwriting (only on a fresh start)
# ============================================================
$oldFile = "experiment_logs\S6_NOISY_BASELINE.jsonl"
if ((Test-Path $oldFile) -and ($StartFromRun -eq 1)) {
    $backupPath = "experiment_logs\S6_NOISY_BASELINE_OLD.jsonl.bak"
    Copy-Item $oldFile $backupPath -Force
    Write-Host "[Setup] Old S6 data backed up to $backupPath" -ForegroundColor Cyan
    Remove-Item $oldFile -Force
    Write-Host "[Setup] Old S6_NOISY_BASELINE.jsonl cleared - fresh data will replace it" -ForegroundColor Cyan
}

# ============================================================
# Main loop
# ============================================================
Start-PortForwards

try {
    for ($runId = $StartFromRun; $runId -le 5; $runId++) {
        Write-Host ""
        Write-Host "============================================================" -ForegroundColor White
        Write-Host " S6_NOISY_BASELINE - RUN $runId / 5 ($CYCLES cycles, control)" -ForegroundColor White
        Write-Host "============================================================" -ForegroundColor White

        $quotaExhausted = $false
        try {
            Inject-S6-NoisyBaseline | Out-Null

            python pipeline\main.py --mode continuous --scenario S6_NOISY_BASELINE --run-id $runId --expected-root-cause none --expected-fault-class none --interval $INTERVAL --runs $CYCLES

            if ($LASTEXITCODE -eq 2) {
                $quotaExhausted = $true
            }
        }
        finally {
            Cleanup-S6-NoisyBaseline
            Wait-ForStableBaseline
        }

        if ($quotaExhausted) {
            Write-Host ""
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " GROQ TOKEN QUOTA EXHAUSTED - PAUSED" -ForegroundColor Red
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " Completed runs 1-$($runId - 1) of S6 successfully." -ForegroundColor Yellow
            Write-Host " Resume with:" -ForegroundColor Yellow
            Write-Host "   .\rerun_s6.ps1 -StartFromRun $runId" -ForegroundColor Cyan
            Write-Host "============================================================" -ForegroundColor Red
            break
        }

        Write-Host "[Progress] S6 run $runId / 5 complete" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "[Experiment] S6_NOISY_BASELINE finished." -ForegroundColor Green
    Write-Host "[Experiment] Data saved to: experiment_logs\S6_NOISY_BASELINE.jsonl"
}
finally {
    Stop-PortForwards
}
