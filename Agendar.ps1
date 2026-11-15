#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    [string]$Repo,
    [ValidatePattern('^[A-Za-z0-9_.-]+\.ya?ml$')]
    [string]$Workflow = 'linux-web-pc.yml',
    [string]$Branch = '',
    [switch]$UmaVez
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'Instale GitHub CLI: winget install GitHub.cli' }
& gh auth status
if ($LASTEXITCODE -ne 0) { throw 'Execute gh auth login primeiro.' }
if (-not $Branch) {
    $Branch = (& gh repo view $Repo --json defaultBranchRef --jq '.defaultBranchRef.name' | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Branch) { throw 'Nao foi possivel consultar a branch padrao.' }
}
& gh workflow enable $Workflow --repo $Repo
if ($LASTEXITCODE -ne 0) { throw 'Publique o workflow na branch padrao antes de agendar.' }

# Com o revezamento ligado (variavel PEER_REPO), as proprias sessoes abrem as proximas.
# Um agendador periodico ao mesmo tempo criaria sessoes duplicadas.
$prevPref = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$peerRepo = (& gh variable get PEER_REPO --repo $Repo 2>$null | Out-String).Trim()
$peerExit = $LASTEXITCODE
$ErrorActionPreference = $prevPref
if ($peerExit -eq 0 -and $peerRepo -and -not $UmaVez) {
    throw ('Revezamento ativo entre ' + $Repo + ' e ' + $peerRepo + '. Use -UmaVez para apenas iniciar o ciclo, ou remova a variavel PEER_REPO para voltar ao agendador periodico.')
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Instale Python 3.10+ e habilite-o no PATH.' }
$alternador = Join-Path $PSScriptRoot 'scripts\alternar_workflow.py'
if (-not (Test-Path -LiteralPath $alternador)) { throw 'Extraia tambem a pasta scripts da versao atualizada.' }

$safeName = ($Repo + '-' + $Workflow) -replace '[^A-Za-z0-9_.-]', '_'
$stateFile = Join-Path $PSScriptRoot ('.scheduler-' + $safeName + '.json')
$mutex = New-Object System.Threading.Mutex($false, ('Local\LinuxWebPC-' + $safeName))
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Ja existe um agendador para este workflow neste computador.' }
    $next = [DateTimeOffset]::UtcNow
    if (Test-Path -LiteralPath $stateFile) {
        $saved = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        $next = [DateTimeOffset]::Parse($saved.nextUtc)
    }
    Write-Host 'Agendador ligado. Mantenha este computador ligado e conectado. Ctrl+C para parar.'
    Write-Host 'Cada disparo cria uma NOVA EXECUCAO do mesmo workflow. Nao cria arquivos YAML.'
    while ($true) {
        $now = [DateTimeOffset]::UtcNow
        if ($now -lt $next) {
            if ($UmaVez) { Write-Host ('Proximo disparo: ' + $next.ToLocalTime()); break }
            $seconds = [Math]::Min(60, [Math]::Max(1, [Math]::Ceiling(($next - $now).TotalSeconds)))
            Start-Sleep -Seconds ([int]$seconds)
            continue
        }
        Write-Host ('Preparando troca de sessao em ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
        & python $alternador --repo $Repo --workflow $Workflow --branch $Branch
        if ($LASTEXITCODE -ne 0) {
            if ($UmaVez) { throw 'A nova execucao nao foi aberta. Confira a execucao antiga e a mensagem acima.' }
            Write-Warning 'A troca nao foi concluida. Nova verificacao em 60 segundos; nenhuma execucao sera sobreposta.'
            Start-Sleep -Seconds 60
            continue
        }
        # Ancora no disparo bem-sucedido. Ao religar, dispara uma vez, sem recuperar backlog.
        $next = [DateTimeOffset]::UtcNow.AddHours(5)
        $state = @{nextUtc=$next.ToString('o'); repo=$Repo; workflow=$Workflow; branch=$Branch}
        $tempFile = $stateFile + '.tmp'
        $state | ConvertTo-Json | Set-Content -LiteralPath $tempFile -Encoding UTF8
        Move-Item -LiteralPath $tempFile -Destination $stateFile -Force
        Write-Host ('Solicitado. Proximo disparo: ' + $next.ToLocalTime())
        if ($UmaVez) { break }
    }
}
finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
