#Requires -Version 5.1
param([Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')][string]$Repo,
      [ValidatePattern('^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?$')][string]$RepoPar = '',
      [switch]$SomenteEmail, [switch]$SomenteRevezamento)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'Instale: winget install GitHub.cli' }
& gh auth status
if ($LASTEXITCODE -ne 0) {
    & gh auth login
    if ($LASTEXITCODE -ne 0) { throw 'Falha no login do GitHub.' }
}
$parArgs = @()
if ($RepoPar) { $parArgs = @('--repo-par', $RepoPar) }
if ($SomenteRevezamento) {
    if (-not $RepoPar) { throw 'Informe -RepoPar USUARIO/REPOSITORIO para o revezamento.' }
    & python (Join-Path $PSScriptRoot 'scripts\configurar.py') --repo $Repo @parArgs --handoff-only
    if ($LASTEXITCODE -ne 0) { throw 'A configuracao do revezamento nao foi concluida.' }
    return
}
if ($SomenteEmail) {
    & python (Join-Path $PSScriptRoot 'scripts\configurar.py') --repo $Repo @parArgs --email-only
    if ($LASTEXITCODE -ne 0) { throw 'A configuracao do email nao foi concluida.' }
    return
}
$venv = Join-Path $PSScriptRoot '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Instale Python 3.10 ou mais novo e habilite-o no PATH.' }
}
& $python -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar dependencias.' }
$configArgs = @((Join-Path $PSScriptRoot 'scripts\configurar.py'), '--repo', $Repo) + $parArgs
if ($SomenteEmail) { $configArgs += '--email-only' }
& $python @configArgs
if ($LASTEXITCODE -ne 0) { throw 'A configuracao nao foi concluida.' }
Write-Host 'Para iniciar o agendador:'
Write-Host ('.\Agendar.ps1 -Repo ' + $Repo)
