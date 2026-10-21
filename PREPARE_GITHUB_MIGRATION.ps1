$ErrorActionPreference = "Stop"

Write-Host "Flight Sentinel V2.9 - preparación del árbol GitHub" -ForegroundColor Cyan

$obsolete = @(
    ".gitlab-ci.yml",
    "GITLAB_AUTOMATION_V2_5.md",
    "GITLAB_CLOUD_FULL_V2_6.md",
    "GITLAB_CLOUD_FULL_V2_7.md"
)

foreach ($path in $obsolete) {
    if (Test-Path $path) {
        Remove-Item -Force $path
        Write-Host "Eliminado: $path"
    }
}

Write-Host "No se tocaron monitors.yaml, .env, bases locales ni la rama flight-state." -ForegroundColor Green
Write-Host "Siguiente paso: revisar git status y ejecutar py -3.14 -m pytest -q" -ForegroundColor Yellow
