# USAGE:
#   .\rerun_s3.ps1
#   .\rerun_s3.ps1 -StartFromRun 4
# ============================================================

param(
    [int]$StartFromRun = 1
)

$ErrorActionPreference = "Continue"
$NAMESPACE = "microservices-demo"
$MONITORING_NS = "monitoring"
$CYCLES = 6           
$INTERVAL = 30
$ALLOC_MB = 90         # RECALIBRATED (real S3 data): confirmed reflection of
                       # allocated memory into container_memory_working_set_bytes
                       # is small and variable (~8-23% of allocated amount
                       # observed across attempts, not close to 100%). 90MB
                       # logical allocation stays safe even in an unobserved
                       # worst-case where reflection approaches 100%: 90MB +
                       # ~20MB baseline = ~110MB, still comfortably under
                       # paymentservice's CONFIRMED real limit of 128Mi
                       # (checked via kubectl describe pod).

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

function Inject-S3-MemoryPressure {
    Write-Host "[Fault] S3: Injecting Node.js heap allocation into paymentservice..." -ForegroundColor Magenta

    # Re-fetch the pod name fresh right before injection (not cached
    # from an earlier check) -- paymentservice has been observed
    # restarting mid-session in this environment, and a stale pod name
    # here silently targets a pod that may no longer exist.
    $pod = kubectl get pod -n $NAMESPACE -l app=paymentservice -o jsonpath='{.items[0].metadata.name}'
    if (-not $pod) {
        Write-Host "  [WARN] paymentservice pod not found" -ForegroundColor Yellow
        return $false
    }

    $testOutput = kubectl exec -n $NAMESPACE $pod -- node -e "console.log('node_test_ok')" 2>&1
    $testOutputJoined = ($testOutput -join "`n")
    Write-Host "  Node test result: $testOutputJoined"

    if ($LASTEXITCODE -ne 0 -or $testOutputJoined -notmatch "node_test_ok") {
        Write-Host "  [FAIL] node binary not usable in this container - injection ABORTED" -ForegroundColor Red
        Write-Host "  Output was: $testOutputJoined" -ForegroundColor Red
        return $false
    }

    Write-Host "  [OK] node available - proceeding with heap allocation ($ALLOC_MB MB)" -ForegroundColor Green

    # FIX #3 (real S3 rerun data): Start-Job + stdin-piping (`$script |
    # kubectl exec -i ...`) appeared to start a node process (visible
    # in `ps aux`) but consistently produced ZERO memory increase
    # across a full 6-cycle run, while a manual synchronous test of
    # the SAME script (run directly in a terminal, no Start-Job, no
    # piping) DID show a real, if partial, memory increase. This
    # points at the Start-Job/pipe delivery mechanism itself as the
    # failure point (likely losing/mangling the piped stdin content
    # across the job boundary), not the allocation script or node
    # itself.
    #
    # FIX #7 (real S3 rerun data): the original duration formula
    # ($CYCLES * $INTERVAL + 30s buffer = 210s for a 6-cycle run)
    # assumed each monitoring cycle takes ~$INTERVAL (30s). Real
    # observed pod_age progression across a confirmed successful run:
    # cycle1=126.3s -> cycle6=425.9s, i.e. ~60s per cycle on average,
    # not 30s -- the 5-vote LLM self-consistency overhead (Groq API
    # latency x5 per cycle) adds real time well beyond the nominal
    # interval. The 210s hold expired around cycle 3-4, causing memory
    # to visibly decline in the back half of the run (confirmed: peak
    # 29.8MB at cycle 3, back down to ~21MB by cycle 4-6) -- the fault
    # was gone for the second half of every run. Recalibrated to use
    # the REAL observed per-cycle pace (~90s, with margin) instead of
    # the nominal interval, plus a larger fixed startup buffer.
    $durationMs = ($CYCLES * 90 + 60) * 1000
    $nodeScript = @"
let a = [];
for (let i = 0; i < $ALLOC_MB; i++) {
    a.push(Buffer.alloc(1024 * 1024));
}
console.log('allocated_' + a.length + 'MB');
setTimeout(() => {}, $durationMs);
"@

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($nodeScript)
    $encoded = [Convert]::ToBase64String($bytes)

    # STEP 1: write the script to a file inside the container,
    # SYNCHRONOUSLY (no backgrounding yet), so a failure here is
    # immediately visible with its own exit code / stderr.
    $writeCmd = "echo $encoded | base64 -d > /tmp/s3_mem.js"
    kubectl exec -n $NAMESPACE $pod -- /bin/sh -c $writeCmd 2>&1 | Out-Null

    # Verify the file actually landed with the expected content before
    # trusting it. IMPORTANT: kubectl's multi-line output comes back as
    # a PowerShell ARRAY of strings (one per line), not one string --
    # -notmatch against an array returns the array of NON-matching
    # LINES (blank lines, closing braces, etc. never contain
    # "Buffer.alloc"), which is non-empty and therefore TRUTHY even
    # when the file is completely correct. Join to a single string
    # first so the match is a real single boolean, not an array-filter
    # false positive.
    $fileCheck = kubectl exec -n $NAMESPACE $pod -- cat /tmp/s3_mem.js 2>&1
    $fileCheckJoined = ($fileCheck -join "`n")
    if ($fileCheckJoined -notmatch "Buffer\.alloc") {
        Write-Host "  [ERROR] Script file write failed or corrupted. Content was:" -ForegroundColor Red
        Write-Host "  $fileCheckJoined"
        return $false
    }
    Write-Host "  [OK] Script file written and verified in pod $pod" -ForegroundColor Green

    # STEP 2: NOW launch it in the background, detached via setsid so
    # it survives even if this exec connection later drops (same
    # reasoning as S1's fix).
    # FIX #5 (real S3 rerun data): this container's BusyBox `setsid`
    # build rejected our invocation with its own usage message, even
    # though the syntax (setsid PROG ARGS) matched its documented
    # form -- a build-specific quirk not worth reverse-engineering
    # further. `nohup` has simpler, more universally consistent
    # syntax across BusyBox variants and achieves the same practical
    # goal here (survive the launching exec connection dropping),
    # since the process is ALSO explicitly backgrounded with '&'
    # inside the remote shell itself.
    # FIX #6 (real S3 rerun data): TWO different, unrelated BusyBox
    # detachment utilities (setsid, then nohup) both failed at this
    # exact step with the same "malformed invocation" symptom, while
    # the SYNCHRONOUS file-write step (plain kubectl exec, no
    # Start-Process) has now succeeded cleanly twice in a row. That
    # pattern points at Start-Process's -ArgumentList handling itself
    # mangling this particular string (likely the trailing '&' or the
    # redirection characters), not at which detachment tool was
    # chosen. Simplified: drop Start-Process AND setsid/nohup
    # entirely, and just run a plain synchronous `kubectl exec` whose
    # REMOTE command backgrounds itself with a trailing '&' inside the
    # shell -- this is exactly the mechanism your own manual test
    # earlier in this session already proved works (a bare `kubectl
    # exec ... -- node -e "..."` with setTimeout holding it open
    # showed a real, measurable memory increase, no special
    # detachment tooling needed). Because the remote shell backgrounds
    # the job with '&', `sh -c "... &"` returns almost immediately on
    # its own -- Start-Process's async wrapper was solving a problem
    # that didn't need solving here.
    $launchCmd = "node /tmp/s3_mem.js > /tmp/s3_mem.log 2>&1 < /dev/null &"
    kubectl exec -n $NAMESPACE $pod -- /bin/sh -c $launchCmd 2>&1 | Out-Null

    Start-Sleep -Seconds 4

    # Verify the node process is ACTUALLY running inside the pod, not
    # just that the local kubectl client process didn't immediately
    # error -- same discipline as S1's Inject-S1-CpuStress.
    $psCheck = kubectl exec -n $NAMESPACE $pod -- ps aux 2>&1
    if ($psCheck -match "node /tmp/s3_mem.js") {
        Write-Host "  [OK] Heap allocation confirmed running in pod $pod ($ALLOC_MB MB, held for ${durationMs}ms)" -ForegroundColor Green
        # surface the script's own stdout (should show allocated_XXMB)
        $logCheck = kubectl exec -n $NAMESPACE $pod -- cat /tmp/s3_mem.log 2>&1
        Write-Host "  Script output: $logCheck"
        return $true
    } else {
        Write-Host "  [ERROR] Heap allocation process NOT found in pod $pod after launch. ps output:" -ForegroundColor Red
        Write-Host "  $psCheck"
        $logCheck = kubectl exec -n $NAMESPACE $pod -- cat /tmp/s3_mem.log 2>&1
        Write-Host "  Script log (if any): $logCheck"
        return $false
    }
}

function Cleanup-S3-MemoryPressure {
    # FIX (real S3 rerun data, root cause finally identified): this
    # function used to force-delete and recreate paymentservice after
    # every run. Run 1 (the only run that ever showed genuine memory
    # elevation) used a pod that already existed BEFORE this script
    # started running -- every subsequent run used a freshly recreated
    # pod, and freshly recreated pods consistently failed to reflect
    # ANY memory elevation in container_memory_working_set_bytes, even
    # with a CONFIRMED successful allocation at the OS/process level
    # (verified via ps aux AND the script's own "allocated_90MB"
    # stdout). The injection was never broken -- recreating the pod
    # between runs was silently breaking the ability to MEASURE it.
    #
    # FIX: stop force-deleting the pod. The Node.js script already
    # expires naturally via its own setTimeout -- when that process
    # exits, its memory is reclaimed immediately (a hard OS guarantee),
    # so there is no need to recreate the pod to "clear" memory. Just
    # verify the previous injection process has actually finished
    # before the next run starts.
    Write-Host "[Cleanup] S3: Verifying previous heap allocation process has exited..." -ForegroundColor DarkGray

    $pod = kubectl get pod -n $NAMESPACE -l app=paymentservice -o jsonpath='{.items[0].metadata.name}'
    if (-not $pod) {
        Write-Host "  [WARN] paymentservice pod not found during cleanup" -ForegroundColor Yellow
        return
    }

    $maxWaitChecks = 8
    for ($i = 1; $i -le $maxWaitChecks; $i++) {
        $psCheck = kubectl exec -n $NAMESPACE $pod -- ps aux 2>&1
        if ($psCheck -notmatch "node /tmp/s3_mem.js") {
            Write-Host "  [OK] Previous allocation process has exited (pod $pod kept alive, not recreated)" -ForegroundColor Green
            return
        }
        Write-Host "  Still running, waiting... ($i/$maxWaitChecks)"
        Start-Sleep -Seconds 15
    }

    # Safety fallback: if the process somehow didn't exit on its own
    # (e.g. setTimeout duration mismatch), kill just that process --
    # NOT the pod itself, so we never recreate it.
    Write-Host "  [WARN] Allocation process still running after ${maxWaitChecks}x15s -- killing it directly (pod stays alive)" -ForegroundColor Yellow
    kubectl exec -n $NAMESPACE $pod -- /bin/sh -c "pkill -f '/tmp/s3_mem.js'" 2>&1 | Out-Null
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
# Backup old S3 data before overwriting
# ============================================================
$oldFile = "experiment_logs\S3_MEMORY_PRESSURE.jsonl"
if ((Test-Path $oldFile) -and ($StartFromRun -eq 1)) {
    $backupPath = "experiment_logs\S3_MEMORY_PRESSURE_OLD_devshm.jsonl.bak"
    Copy-Item $oldFile $backupPath -Force
    Write-Host "[Setup] Old /dev/shm-based S3 data backed up to $backupPath" -ForegroundColor Cyan
    Remove-Item $oldFile -Force
    Write-Host "[Setup] Old S3_MEMORY_PRESSURE.jsonl cleared - fresh Node.js-heap data will replace it" -ForegroundColor Cyan
}

# ============================================================
# Main loop
# ============================================================
Start-PortForwards

try {
    for ($runId = $StartFromRun; $runId -le 5; $runId++) {
        Write-Host ""
        Write-Host "============================================================" -ForegroundColor White
        Write-Host " S3_MEMORY_PRESSURE - RUN $runId / 5 (Node.js heap fix)" -ForegroundColor White
        Write-Host "============================================================" -ForegroundColor White

        $quotaExhausted = $false
        try {
            $injected = Inject-S3-MemoryPressure

            if (-not $injected) {
                Write-Host "[Pipeline] SKIPPING this run - injection failed, would produce bad data" -ForegroundColor Red
                Start-Sleep -Seconds 5
                continue
            }

            python pipeline\main.py --mode continuous --scenario S3_MEMORY_PRESSURE --run-id $runId --expected-root-cause paymentservice --expected-fault-class resource --interval $INTERVAL --runs $CYCLES

            if ($LASTEXITCODE -eq 2) {
                $quotaExhausted = $true
            }
        }
        finally {
            Cleanup-S3-MemoryPressure
            Wait-ForStableBaseline
        }

        if ($quotaExhausted) {
            Write-Host ""
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " GROQ TOKEN QUOTA EXHAUSTED - PAUSED" -ForegroundColor Red
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " Completed runs 1-$($runId - 1) of S3 successfully." -ForegroundColor Yellow
            Write-Host " Resume with:" -ForegroundColor Yellow
            Write-Host "   .\rerun_s3.ps1 -StartFromRun $runId" -ForegroundColor Cyan
            Write-Host "============================================================" -ForegroundColor Red
            break
        }

        Write-Host "[Progress] S3 run $runId / 5 complete" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "[Experiment] S3_MEMORY_PRESSURE re-run finished." -ForegroundColor Green
    Write-Host "[Experiment] Data saved to: experiment_logs\S3_MEMORY_PRESSURE.jsonl"
}
finally {
    Stop-PortForwards
}