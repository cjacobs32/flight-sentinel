param(
    [string]$GitHubRepo = "https://github.com/cjacobs32/flight-sentinel-miami.git"
)

$ErrorActionPreference = "Stop"

function Run-Git {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)
    & git @Args
    if ($LASTEXITCODE -ne 0) { throw "git $($Args -join ' ') falló con código $LASTEXITCODE" }
}

Write-Host "Flight Sentinel V2.9 - migración GitLab -> GitHub" -ForegroundColor Cyan

if (-not (Test-Path ".git")) {
    throw "Ejecutá este script desde la raíz del clon Git actual."
}

$status = git status --porcelain
if ($LASTEXITCODE -ne 0) { throw "No se pudo leer git status." }
if ($status) {
    throw "Hay cambios sin commit. Primero validá y commiteá V2.9; luego repetí este script."
}

$currentOrigin = (git remote get-url origin 2>$null)
if (-not $currentOrigin) { throw "No existe remote origin." }

Write-Host "Remote actual: $currentOrigin"
Write-Host "Recuperando main y flight-state antes de cambiar remotos..."
Run-Git fetch origin main flight-state

$remotes = git remote
if ($remotes -contains "gitlab") {
    Write-Host "El remote gitlab ya existe; no se renombra origin." -ForegroundColor Yellow
} else {
    Run-Git remote rename origin gitlab
}

$remotes = git remote
if ($remotes -contains "origin") {
    Run-Git remote set-url origin $GitHubRepo
} else {
    Run-Git remote add origin $GitHubRepo
}

Write-Host "Comprobando acceso al repositorio GitHub..."
& git ls-remote origin HEAD *> $null
if ($LASTEXITCODE -ne 0) {
    throw "No se pudo acceder a $GitHubRepo. Creá el repo en GitHub y verificá tu autenticación antes de continuar."
}

Write-Host "Subiendo main..."
Run-Git push -u origin main

Write-Host "Subiendo flight-state con el histórico actual..."
Run-Git push origin refs/remotes/gitlab/flight-state:refs/heads/flight-state

Write-Host "Migración Git completada. No borres el remote gitlab hasta validar Pages, SMTP y WATCH." -ForegroundColor Green
Write-Host "Remotos:" -ForegroundColor Cyan
& git remote -v
