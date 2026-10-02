# USAGE:
#   .\rerun_s2.ps1
#   .\rerun_s2.ps1 -StartFromRun 3
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

function Inject-S2-PodCrash {
    Write-Host "[Fault] S2: Force-deleting cartservice pod..." -ForegroundColor Magenta

    # Verify the pod exists before deleting, so a genuinely missing
    # deployment is caught here rather than silently producing an
    # empty/meaningless run (same discipline as S1/S3's pre-checks).
    $pod = kubectl get pod -n $NAMESPACE -l app=cartservice -o jsonpath='{.items[0].metadata.name}'
    if (-not $pod) {
        Write-Host "  [WARN] cartservice pod not found -- cannot inject S2" -ForegroundColor Yellow
        return $false
    }

    kubectl delete pod -n $NAMESPACE -l app=cartservice --grace-period=0 --force 2>$null | Out-Null
    Write-Host "  Force-deleted pod $pod -- Kubernetes will recreate via Deployment"

    # Give the Deployment controller a moment to schedule the
    # replacement, then confirm a NEW pod is actually coming up before
    # telling the caller injection succeeded.
    Start-Sleep -Seconds 3
    $newPod = kubectl get pod -n $NAMESPACE -l app=cartservice -o jsonpath='{.items[0].metadata.name}'
    if (-not $newPod) {
        Write-Host "  [ERROR] No replacement pod found after delete -- injection may have failed" -ForegroundColor Red
        return $false
    }
    if ($newPod -eq $pod) {
        Write-Host "  [WARN] Pod name unchanged ($newPod) -- delete may not have taken effect yet" -ForegroundColor Yellow
    } else {
        Write-Host "  [OK] Replacement pod scheduled: $newPod" -ForegroundColor Green
    }
    return $true
}

function Cleanup-S2-PodCrash {
    Write-Host "[Cleanup] S2: Waiting for cartservice to stabilise..." -ForegroundColor DarkGray
    kubectl rollout status deployment/cartservice -n $NAMESPACE --timeout=90s 2>$null | Out-Null
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
# Backup old S2 data before overwriting (only on a fresh start)
# ============================================================
$oldFile = "experiment_logs\S2_POD_CRASH.jsonl"
if ((Test-Path $oldFile) -and ($StartFromRun -eq 1)) {
    $backupPath = "experiment_logs\S2_POD_CRASH_OLD.jsonl.bak"
    Copy-Item $oldFile $backupPath -Force
    Write-Host "[Setup] Old S2 data backed up to $backupPath" -ForegroundColor Cyan
    Remove-Item $oldFile -Force
    Write-Host "[Setup] Old S2_POD_CRASH.jsonl cleared - fresh data will replace it" -ForegroundColor Cyan
}

# ============================================================
# Main loop
# ============================================================
Start-PortForwards

try {
    for ($runId = $StartFromRun; $runId -le 5; $runId++) {
        Write-Host ""
        Write-Host "============================================================" -ForegroundColor White
        Write-Host " S2_POD_CRASH - RUN $runId / 5 ($CYCLES cycles)" -ForegroundColor White
        Write-Host "============================================================" -ForegroundColor White

        $quotaExhausted = $false
        try {
            $injected = Inject-S2-PodCrash

            if (-not $injected) {
                Write-Host "[Pipeline] SKIPPING this run - injection failed, would produce bad data" -ForegroundColor Red
                Start-Sleep -Seconds 5
                continue
            }

            python pipeline\main.py --mode continuous --scenario S2_POD_CRASH --run-id $runId --expected-root-cause cartservice --expected-fault-class crash --interval $INTERVAL --runs $CYCLES

            if ($LASTEXITCODE -eq 2) {
                $quotaExhausted = $true
            }
        }
        finally {
            Cleanup-S2-PodCrash
            Wait-ForStableBaseline
        }

        if ($quotaExhausted) {
            Write-Host ""
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " GROQ TOKEN QUOTA EXHAUSTED - PAUSED" -ForegroundColor Red
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " Completed runs 1-$($runId - 1) of S2 successfully." -ForegroundColor Yellow
            Write-Host " Resume with:" -ForegroundColor Yellow
            Write-Host "   .\rerun_s2.ps1 -StartFromRun $runId" -ForegroundColor Cyan
            Write-Host "============================================================" -ForegroundColor Red
            break
        }

        Write-Host "[Progress] S2 run $runId / 5 complete" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "[Experiment] S2_POD_CRASH re-run finished." -ForegroundColor Green
    Write-Host "[Experiment] Data saved to: experiment_logs\S2_POD_CRASH.jsonl"
}
finally {
    Stop-PortForwards
}
