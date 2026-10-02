# USAGE:
#   .\rerun_s4.ps1
#   .\rerun_s4.ps1 -StartFromRun 3
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

function Inject-S4-NetworkLatency {
    Write-Host "[Fault] S4: Injecting 500ms network latency into frontend..." -ForegroundColor Magenta
    $pod = kubectl get pod -n $NAMESPACE -l app=frontend -o jsonpath='{.items[0].metadata.name}'
    if (-not $pod) {
        Write-Host "  [WARN] frontend pod not found" -ForegroundColor Yellow
        return $false
    }

    # frontend's own container has no tc binary (minimal image,
    # confirmed via direct test on cartservice earlier -- frontend
    # is built the same minimal way). Attach an ephemeral debug
    # container (netshoot image, has tc/iproute2) sharing frontend's
    # network namespace -- a netem rule applied from netshoot affects
    # frontend's real traffic even though frontend's own container
    # never has tc installed.
    #
    # TARGET CHANGED FROM cartservice TO frontend (real S4 debugging,
    # this session): systematically confirmed that cartservice's
    # deployed image (gcr.io/google-samples/microservices-demo,
    # v0.8.0 and v0.8.1) does NOT implement OpenTelemetry trace
    # export in this repo lineage -- confirmed via a still-open
    # GitHub tracking issue ("Add OpenTelemetry Trace to C#
    # cartservice", cloud-ops-sandbox#726) requesting exactly this
    # unimplemented capability. frontend (Go), by contrast, has
    # confirmed working COLLECTOR_SERVICE_ADDR-based tracing (source:
    # frontend/main.go calls mustMapEnv(&svc.collectorAddr,
    # "COLLECTOR_SERVICE_ADDR")) -- verified end-to-end tonight with
    # real trace data appearing in Jaeger after setting
    # COLLECTOR_SERVICE_ADDR, ENABLE_TRACING, ENABLE_STATS, and
    # OTEL_SERVICE_NAME on the frontend deployment. frontend is also
    # a better fault target for THIS reason: it's the service the
    # load generator calls directly, so its own trace data most
    # directly reflects induced latency.
    #
    # IMPORTANT: this script assumes the frontend Deployment already
    # has COLLECTOR_SERVICE_ADDR=jaeger.monitoring.svc.cluster.local:4317,
    # ENABLE_TRACING=1, ENABLE_STATS=1, and OTEL_SERVICE_NAME=frontend
    # set (via `kubectl set env`, done once tonight). These persist in
    # the Deployment spec across pod restarts/recreations, so
    # Cleanup-S4-NetworkLatency's pod-recreation does NOT need to
    # re-apply them -- but if frontend is ever redeployed from a
    # fresh manifest, they must be re-applied first.
    # FIX (real S4 pilot data): kubectl debug -- tc qdisc add runs tc
    # as a ONE-SHOT command -- the container exits immediately after
    # (success or failure), whether or not the rule applied. Combine
    # applying the rule with staying alive afterward, so there is
    # something running to verify against. The tc rule itself attaches
    # to the shared network namespace, not to this container's
    # lifecycle, so it persists regardless -- but we need a live
    # process to query it with.
    $result = kubectl debug -n $NAMESPACE $pod --image=nicolaka/netshoot --target=frontend --profile=sysadmin -- /bin/sh -c "tc qdisc add dev eth0 root netem delay 500ms; sleep 3600" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [FAIL] kubectl debug command failed to launch on pod $pod" -ForegroundColor Red
        Write-Host "  Error output: $result" -ForegroundColor Red
        return $false
    }

    # Poll for the ephemeral container to actually register in the
    # pod's spec, rather than trusting a fixed sleep -- get its REAL
    # assigned name directly instead of assuming a requested one took.
    $debugContainerName = $null
    for ($i = 1; $i -le 10; $i++) {
        Start-Sleep -Seconds 2
        $debugContainerName = kubectl get pod -n $NAMESPACE $pod -o jsonpath='{.spec.ephemeralContainers[-1:].name}' 2>$null
        if ($debugContainerName) {
            break
        }
        Write-Host "  Waiting for ephemeral debug container to register... ($i/10)"
    }

    if (-not $debugContainerName) {
        Write-Host "  [ERROR] No ephemeral container ever appeared in pod spec after tc qdisc add" -ForegroundColor Red
        return $false
    }
    Write-Host "  Debug container registered in spec as: $debugContainerName"

    # FIX (real S4 pilot data, third attempt): being present in
    # .spec.ephemeralContainers is NOT the same as actually being
    # RUNNING -- confirmed real failure mode: name found in spec
    # ("debugger-rw5st"), yet `kubectl exec` into it immediately said
    # "container not found". Poll .status.ephemeralContainerStatuses
    # for that SPECIFIC container's running state before trying to
    # exec into it, instead of trusting spec registration alone.
    $isRunning = $false
    for ($i = 1; $i -le 10; $i++) {
        $statusCheck = kubectl get pod -n $NAMESPACE $pod -o jsonpath="{.status.ephemeralContainerStatuses[?(@.name=='$debugContainerName')].state.running}" 2>$null
        if ($statusCheck) {
            $isRunning = $true
            break
        }
        Write-Host "  Waiting for $debugContainerName to reach Running state... ($i/10)"
        Start-Sleep -Seconds 2
    }

    if (-not $isRunning) {
        Write-Host "  [ERROR] $debugContainerName never reached Running state" -ForegroundColor Red
        $fullStatus = kubectl get pod -n $NAMESPACE $pod -o jsonpath="{.status.ephemeralContainerStatuses}" 2>$null
        Write-Host "  Full ephemeral container status: $fullStatus" -ForegroundColor Red
        return $false
    }
    Write-Host "  [OK] $debugContainerName confirmed Running" -ForegroundColor Green

    $verify = kubectl exec -n $NAMESPACE $pod -c $debugContainerName -- tc qdisc show dev eth0 2>&1
    $verifyJoined = ($verify -join "`n")
    if ($verifyJoined -match "netem" -and $verifyJoined -match "500") {
        Write-Host "  [OK] tc netem CONFIRMED active on pod $pod (verified via kubectl exec into $debugContainerName)" -ForegroundColor Green
        Write-Host "  qdisc state: $verifyJoined"
        return $true
    } else {
        Write-Host "  [ERROR] tc qdisc add reported success, but verification did NOT show an active netem/500ms rule" -ForegroundColor Red
        Write-Host "  qdisc state was: $verifyJoined" -ForegroundColor Red
        return $false
    }
}

function Cleanup-S4-NetworkLatency {
    # Ephemeral debug containers cannot be cleanly removed by
    # Kubernetes (a known limitation) -- restarting the pod is the
    # only way to fully clear both the tc rule and the debug
    # container. NOTE: this is the exact "recreate pod between runs"
    # pattern that broke S3's measurement for an entire session. We
    # have NOT yet confirmed whether S4 has an equivalent issue --
    # validate run 1 and run 2 individually before trusting further
    # runs to behave the same way unattended.
    Write-Host "[Cleanup] S4: Restarting frontend to clear tc rule + debug container..." -ForegroundColor DarkGray
    kubectl delete pod -n $NAMESPACE -l app=frontend --grace-period=0 --force 2>$null | Out-Null
    kubectl rollout status deployment/frontend -n $NAMESPACE --timeout=90s 2>$null | Out-Null
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
# Backup old S4 data before overwriting (only on a fresh start)
# ============================================================
$oldFile = "experiment_logs\S4_NETWORK_LATENCY.jsonl"
if ((Test-Path $oldFile) -and ($StartFromRun -eq 1)) {
    $backupPath = "experiment_logs\S4_NETWORK_LATENCY_OLD.jsonl.bak"
    Copy-Item $oldFile $backupPath -Force
    Write-Host "[Setup] Old S4 data backed up to $backupPath" -ForegroundColor Cyan
    Remove-Item $oldFile -Force
    Write-Host "[Setup] Old S4_NETWORK_LATENCY.jsonl cleared - fresh data will replace it" -ForegroundColor Cyan
}

# ============================================================
# Main loop
# ============================================================
Start-PortForwards

try {
    for ($runId = $StartFromRun; $runId -le 5; $runId++) {
        Write-Host ""
        Write-Host "============================================================" -ForegroundColor White
        Write-Host " S4_NETWORK_LATENCY - RUN $runId / 5 ($CYCLES cycles)" -ForegroundColor White
        Write-Host "============================================================" -ForegroundColor White

        $quotaExhausted = $false
        try {
            $injected = Inject-S4-NetworkLatency

            if (-not $injected) {
                Write-Host "[Pipeline] SKIPPING this run - injection failed or unverified, would produce bad data" -ForegroundColor Red
                Start-Sleep -Seconds 5
                continue
            }

            python pipeline\main.py --mode continuous --scenario S4_NETWORK_LATENCY --run-id $runId --expected-root-cause frontend --expected-fault-class network --interval $INTERVAL --runs $CYCLES

            if ($LASTEXITCODE -eq 2) {
                $quotaExhausted = $true
            }
        }
        finally {
            Cleanup-S4-NetworkLatency
            Wait-ForStableBaseline
        }

        if ($quotaExhausted) {
            Write-Host ""
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " GROQ TOKEN QUOTA EXHAUSTED - PAUSED" -ForegroundColor Red
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " Completed runs 1-$($runId - 1) of S4 successfully." -ForegroundColor Yellow
            Write-Host " Resume with:" -ForegroundColor Yellow
            Write-Host "   .\rerun_s4.ps1 -StartFromRun $runId" -ForegroundColor Cyan
            Write-Host "============================================================" -ForegroundColor Red
            break
        }

        Write-Host "[Progress] S4 run $runId / 5 complete" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "[Experiment] S4_NETWORK_LATENCY re-run finished." -ForegroundColor Green
    Write-Host "[Experiment] Data saved to: experiment_logs\S4_NETWORK_LATENCY.jsonl"
}
finally {
    Stop-PortForwards
}