# ============================================================
# rerun_s5.ps1
#
# S5_CASCADING_FAILURE - force-deletes redis-cart, a dependency
# cartservice relies on for cart storage. Mechanically simple
# (same force-delete pattern as S2's pod crash), but the DETECTION
# challenge is different: redis-cart itself is NOT a monitored
# service (metric_collector.py and log_parser.py both only track
# the 5 app services), so the only observable signal is the
# DOWNSTREAM effect on cartservice (and possibly frontend) --
# specifically, cartservice's own error logs when it can't reach
# Redis. This is why log_parser.py and context_builder.py were
# patched tonight to surface dependency-specific keywords (e.g.
# "redis") from real error log text, rather than only a generic
# "ERROR pattern" bucket count with no indication of WHY.
#
# expected_root_cause_service is "redis-cart" (the TRUE root
# cause) even though the actual anomaly signal will show up on
# cartservice -- this directly tests whether the fixed log-based
# dependency-hint pipeline correctly attributes root cause to the
# failed dependency rather than the service that merely couldn't
# reach it (the paper's own documented E2 misattribution finding).
#
# 12 CYCLES: matching S1/S2/S4's convention. Cascading effects
# (cartservice failing, then recovering once redis-cart is back)
# need time to develop and resolve, similar to S2's crash-recovery
# window.
#
# USAGE:
#   .\rerun_s5.ps1
#   .\rerun_s5.ps1 -StartFromRun 3
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

function Inject-S5-CascadingFailure {
    Write-Host "[Fault] S5: Force-deleting redis-cart..." -ForegroundColor Magenta

    $pod = kubectl get pod -n $NAMESPACE -l app=redis-cart -o jsonpath='{.items[0].metadata.name}'
    if (-not $pod) {
        Write-Host "  [WARN] redis-cart pod not found -- cannot inject S5" -ForegroundColor Yellow
        return $false
    }

    kubectl delete pod -n $NAMESPACE -l app=redis-cart --grace-period=0 --force 2>$null | Out-Null
    Write-Host "  Force-deleted pod $pod -- Kubernetes will recreate via Deployment"

    # Verify a replacement pod actually gets scheduled, same discipline
    # as every other injection function tonight -- don't just trust
    # the delete succeeded.
    Start-Sleep -Seconds 3
    $newPod = kubectl get pod -n $NAMESPACE -l app=redis-cart -o jsonpath='{.items[0].metadata.name}'
    if (-not $newPod) {
        Write-Host "  [ERROR] No replacement redis-cart pod found after delete" -ForegroundColor Red
        return $false
    }
    Write-Host "  [OK] Replacement redis-cart pod scheduled: $newPod" -ForegroundColor Green
    return $true
}

function Cleanup-S5-CascadingFailure {
    Write-Host "[Cleanup] S5: Waiting for redis-cart to stabilise..." -ForegroundColor DarkGray
    kubectl rollout status deployment/redis-cart -n $NAMESPACE --timeout=90s 2>$null | Out-Null
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
# Backup old S5 data before overwriting (only on a fresh start)
# ============================================================
$oldFile = "experiment_logs\S5_CASCADING_FAILURE.jsonl"
if ((Test-Path $oldFile) -and ($StartFromRun -eq 1)) {
    $backupPath = "experiment_logs\S5_CASCADING_FAILURE_OLD.jsonl.bak"
    Copy-Item $oldFile $backupPath -Force
    Write-Host "[Setup] Old S5 data backed up to $backupPath" -ForegroundColor Cyan
    Remove-Item $oldFile -Force
    Write-Host "[Setup] Old S5_CASCADING_FAILURE.jsonl cleared - fresh data will replace it" -ForegroundColor Cyan
}

# ============================================================
# Main loop  (PILOT: -le 2 -- change to -le 5 once validated)
# ============================================================
Start-PortForwards

try {
    for ($runId = $StartFromRun; $runId -le 5; $runId++) {
        Write-Host ""
        Write-Host "============================================================" -ForegroundColor White
        Write-Host " S5_CASCADING_FAILURE - RUN $runId (pilot) ($CYCLES cycles)" -ForegroundColor White
        Write-Host "============================================================" -ForegroundColor White

        $quotaExhausted = $false
        try {
            $injected = Inject-S5-CascadingFailure

            if (-not $injected) {
                Write-Host "[Pipeline] SKIPPING this run - injection failed, would produce bad data" -ForegroundColor Red
                Start-Sleep -Seconds 5
                continue
            }

            python pipeline\main.py --mode continuous --scenario S5_CASCADING_FAILURE --run-id $runId --expected-root-cause redis-cart --expected-fault-class dependency --interval $INTERVAL --runs $CYCLES

            if ($LASTEXITCODE -eq 2) {
                $quotaExhausted = $true
            }
        }
        finally {
            Cleanup-S5-CascadingFailure
            Wait-ForStableBaseline
        }

        if ($quotaExhausted) {
            Write-Host ""
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " GROQ TOKEN QUOTA EXHAUSTED - PAUSED" -ForegroundColor Red
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " Completed runs 1-$($runId - 1) of S5 successfully." -ForegroundColor Yellow
            Write-Host " Resume with:" -ForegroundColor Yellow
            Write-Host "   .\rerun_s5.ps1 -StartFromRun $runId" -ForegroundColor Cyan
            Write-Host "============================================================" -ForegroundColor Red
            break
        }

        Write-Host "[Progress] S5 run $runId complete" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "[Experiment] S5_CASCADING_FAILURE pilot finished." -ForegroundColor Green
    Write-Host "[Experiment] Data saved to: experiment_logs\S5_CASCADING_FAILURE.jsonl"
}
finally {
    Stop-PortForwards
}
