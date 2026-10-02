# USAGE:
#   .\rerun_s1.ps1
#   .\rerun_s1.ps1 -StartFromRun 4   (agar beech mein rukna pade)
# ============================================================

param(
    [int]$StartFromRun = 1
)

$ErrorActionPreference = "Continue"
$NAMESPACE = "microservices-demo"
$MONITORING_NS = "monitoring"
$CYCLES = 12          # CHANGED: 6 -> 12 (6 min fault duration)
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

 
function Inject-S1-CpuStress {
    Write-Host "[Fault] S1: Injecting CPU stress into cartservice (extended duration)..." -ForegroundColor Magenta
    $pod = kubectl get pod -n $NAMESPACE -l app=cartservice -o jsonpath='{.items[0].metadata.name}'
    if (-not $pod) {
        Write-Host "  [WARN] cartservice pod not found" -ForegroundColor Yellow
        return $false
    }
 
    $detachedCmd = 'setsid /bin/sh -c ''i=0; while true; do i=$((i+1)); done'' >/dev/null 2>&1 </dev/null &'
    kubectl exec -n $NAMESPACE $pod -- /bin/sh -c $detachedCmd 2>&1 | Out-Null
 
    Start-Sleep -Seconds 3
    $psCheck = kubectl exec -n $NAMESPACE $pod -- ps aux 2>&1
    if ($psCheck -match "while") {
        Write-Host "  [OK] Detached busy-loop confirmed running in pod $pod" -ForegroundColor Green
        return $true
    } else {
        Write-Host "  [ERROR] Busy-loop did not start. ps output:" -ForegroundColor Red
        Write-Host "  $psCheck"
        return $false
    }
}
 

function Cleanup-S1-CpuStress {
    Write-Host "[Cleanup] S1: Restarting cartservice to clear busy-loop..." -ForegroundColor DarkGray
    kubectl delete pod -n $NAMESPACE -l app=cartservice --grace-period=0 --force 2>$null | Out-Null
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
# Backup old S1 data before overwriting (safety net)
# ============================================================
$oldFile = "experiment_logs\S1_CPU_STRESS.jsonl"
if ((Test-Path $oldFile) -and ($StartFromRun -eq 1)) {
    $backupPath = "experiment_logs\S1_CPU_STRESS_OLD_6cycle.jsonl.bak"
    Copy-Item $oldFile $backupPath -Force
    Write-Host "[Setup] Old 6-cycle S1 data backed up to $backupPath" -ForegroundColor Cyan
    Remove-Item $oldFile -Force
    Write-Host "[Setup] Old S1_CPU_STRESS.jsonl cleared - fresh 12-cycle data will replace it" -ForegroundColor Cyan
}

# ============================================================
# Main loop: 10 runs, 12 cycles each
# ============================================================
Start-PortForwards

try {
    for ($runId = $StartFromRun; $runId -le 5; $runId++) {
        Write-Host ""
        Write-Host "============================================================" -ForegroundColor White
        Write-Host " S1_CPU_STRESS - RUN $runId / 10 (12 cycles, 6 min duration)" -ForegroundColor White
        Write-Host "============================================================" -ForegroundColor White

        $quotaExhausted = $false
        try {
            Inject-S1-CpuStress

            python pipeline\main.py --mode continuous --scenario S1_CPU_STRESS --run-id $runId --expected-root-cause cartservice --expected-fault-class resource --interval $INTERVAL --runs $CYCLES

            if ($LASTEXITCODE -eq 2) {
                $quotaExhausted = $true
            }
        }
        finally {
            Cleanup-S1-CpuStress
            Wait-ForStableBaseline
        }

        if ($quotaExhausted) {
            Write-Host ""
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " GROQ DAILY TOKEN QUOTA EXHAUSTED - PAUSED" -ForegroundColor Red
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " Completed runs 1-$($runId - 1) of S1 successfully." -ForegroundColor Yellow
            Write-Host " Resume tomorrow with:" -ForegroundColor Yellow
            Write-Host "   .\rerun_s1.ps1 -StartFromRun $runId" -ForegroundColor Cyan
            Write-Host "============================================================" -ForegroundColor Red
            break
        }

        Write-Host "[Progress] S1 run $runId / 10 complete" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "[Experiment] S1_CPU_STRESS re-run finished." -ForegroundColor Green
    Write-Host "[Experiment] Data saved to: experiment_logs\S1_CPU_STRESS.jsonl"
}
finally {
    Stop-PortForwards
}