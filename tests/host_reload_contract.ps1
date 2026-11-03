param(
    [Parameter(Mandatory)][string]$CandidateDirectory,
    [Parameter(Mandatory)][string]$FixtureDirectory,
    [Parameter(Mandatory)][string]$TemplateDirectory,
    [Parameter(Mandatory)][string]$ResultsDirectory,
    [string[]]$Modes = @('default', 'hybrid', 'off', 'hot', 'restart', 'prebuilt-hot')
)
$ErrorActionPreference = 'Stop'
$image = 'sha256:b2b018808de57ce97219fd25b62cc0034731e7fae2e0dc987ea14491b037e309'
$label = 'am-host-reload-candidate-20261026'
New-Item -ItemType Directory -Path $ResultsDirectory -ErrorAction Stop | Out-Null
function D {
    & docker --context desktop-linux @args
    if ($LASTEXITCODE -ne 0) { throw "Docker failed: $($args[0])" }
}
function Wait-Ready {
    for ($i = 0; $i -lt 90; $i++) {
        & docker --context desktop-linux exec $name python3 -c 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:18789/health",timeout=2)' 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 1
    }
    throw 'Startup/readiness timeout (not a configuration-effectiveness proof)'
}
function Start-Fixture { D exec -d $name node /probe/model-server.mjs }
$results = @()
foreach ($mode in $Modes) {
    $name = 'am-host-reload-' + [Guid]::NewGuid().ToString('N')
    $volume = $name + '-state'
    try {
        D volume create --label "unitag.test=$label" $volume | Out-Null
        D run --rm --pull never --network none --user root --mount "type=volume,source=$volume,target=/state" --entrypoint chown $image 1000:1000 /state
        D run -d --pull never --network none --cpus 2 --memory 2g --pids-limit 256 --name $name --label "unitag.test=$label" `
            --mount "type=volume,source=$volume,target=/home/node/.openclaw,volume-nocopy" `
            --mount "type=bind,source=$CandidateDirectory,target=/opt/unitag-agent-manager/current,readonly" `
            --mount "type=bind,source=$FixtureDirectory,target=/probe,readonly" `
            --mount "type=bind,source=$TemplateDirectory,target=/fixtures,readonly" `
            --env OPENCLAW_GATEWAY_TOKEN=synthetic-host-reload-only `
            --env UNITAG_AGENT_MANAGER_RUNTIME=container `
            --env UNITAG_AGENT_MANAGER_CONTROL_UI_ORIGIN=https://image-test.invalid $image | Out-Null
        Write-Output "START mode=$mode container=$name"
        Wait-Ready
        D exec --env PYTHONPATH=/opt/unitag-agent-manager/current $name python3 -c 'import agent_manage;from agent_manage.restart_policy import standard_host_reload_available;print(agent_manage.__file__);assert standard_host_reload_available("openclaw",container_runtime=True)' | Out-File (Join-Path $ResultsDirectory "$mode-source.log")
        if ($mode -ne 'default') {
            $configuredMode = if ($mode -eq 'prebuilt-hot') { 'hot' } else { $mode }
            D exec --env "FIXTURE_RELOAD_MODE=$configuredMode" $name python3 -c 'import json,pathlib,os;p=pathlib.Path("/home/node/.openclaw/openclaw.json");c=json.loads(p.read_text());c.setdefault("gateway",{})["reload"]={"mode":os.environ["FIXTURE_RELOAD_MODE"]};p.write_text(json.dumps(c))'
            D restart --time 30 $name | Out-Null
            Wait-Ready
        }
        Start-Fixture
        if ($mode -eq 'prebuilt-hot') {
            D exec --env PYTHONPATH=/opt/unitag-agent-manager/current $name python3 /probe/configure.py add-agent | Out-File (Join-Path $ResultsDirectory "$mode-preinstall.log")
            $stages = @('configure-instance')
        } else {
            $stages = @('create-instance', 'add-agent', 'configure-instance', 'configure-noop')
        }
        foreach ($stage in $stages) {
            $command = if ($stage -eq 'configure-noop') { 'configure-instance' } else { $stage }
            $updated = $command -eq 'configure-instance'
            $key = if ($updated) { 'synthetic-model-key-v2' } else { 'synthetic-model-key' }
            $generation = if ($updated) { 'updated' } else { 'original' }
            $agent = if ($command -eq 'create-instance') { 'dola-agent-trial' } else { 'dola-agent-basic' }
            $expected = $stage -ne 'configure-noop' -and ($mode -eq 'off' -or $mode -eq 'prebuilt-hot' -or ($mode -eq 'hot' -and $command -eq 'create-instance'))
            $before = D inspect --format '{{.Id}} {{.State.StartedAt}} {{.State.Pid}}' $name
            $output = D exec --env PYTHONPATH=/opt/unitag-agent-manager/current --env "FIXTURE_MODEL_KEY=$key" $name python3 /probe/configure.py $command
            $cli = ($output -join "`n") | ConvertFrom-Json
            $output | Out-File (Join-Path $ResultsDirectory "$mode-$stage-cli.json")
            if ($cli.restartRequired -ne $expected) { throw "Unexpected flag: $mode/$stage expected=$expected actual=$($cli.restartRequired)" }
            if ($cli.restartRequired) {
                D restart --time 30 $name | Out-Null
                Wait-Ready
                Start-Fixture
            }
            # Stabilization is deliberately test-only; the CLI makes no synchronous readiness claim.
            Start-Sleep -Seconds 8
            Wait-Ready
            D exec --env "EXPECTED_AGENT=$agent" --env "EXPECTED_KEY_GENERATION=$generation" $name node /probe/probe.mjs | Out-File (Join-Path $ResultsDirectory "$mode-$stage-proof.log")
            $after = D inspect --format '{{.Id}} {{.State.StartedAt}} {{.State.Pid}}' $name
            if (-not $cli.restartRequired -and $before -cne $after) { throw 'Unrequested container restart' }
            if ($cli.restartRequired -and $before -ceq $after) { throw 'Fallback container restart was not observed' }
            $results += [pscustomobject]@{mode=$mode;stage=$stage;restartRequired=$cli.restartRequired;before=$before;after=$after;liveProof=$true;imageId=$image}
            $results | ConvertTo-Json -Depth 5 | Out-File (Join-Path $ResultsDirectory 'results.json')
            Write-Output "PASS $mode/$stage restartRequired=$($cli.restartRequired)"
        }
        D exec $name nginx -t -c /home/node/.openclaw/nginx/nginx.conf
        D exec $name python3 -c 'from pathlib import Path;import urllib.request;p=Path("/home/node/.openclaw/workspace/public/reload-probe.txt");p.write_text("host-reload-ok");assert urllib.request.urlopen("http://127.0.0.1/reload-probe.txt",timeout=3).read()==b"host-reload-ok"'
        Write-Output "PASS $mode nginx and website"
    } finally {
        $owner = & docker --context desktop-linux inspect --format '{{index .Config.Labels "unitag.test"}}' $name 2>$null
        if ($LASTEXITCODE -eq 0 -and $owner -eq $label) {
            & docker --context desktop-linux logs --timestamps $name 2>&1 | Out-File (Join-Path $ResultsDirectory "$mode-container.log")
            D rm --force $name | Out-Null
        }
        $owner = & docker --context desktop-linux volume inspect --format '{{index .Labels "unitag.test"}}' $volume 2>$null
        if ($LASTEXITCODE -eq 0 -and $owner -eq $label) { D volume rm $volume | Out-Null }
        Write-Output "CLEANED $name"
    }
}
