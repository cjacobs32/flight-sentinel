# Flight Sentinel V2.9 — Migración a GitHub Actions

## Objetivo

Migrar la operación cloud completa de Flight Sentinel a GitHub sin perder monitores, histórico ni el guardia de cuota, y manteniendo costo total **$0**.

## Arquitectura

- `main`: código + `monitors.yaml`.
- `flight-state`: `state.json` durable.
- GitHub Actions: CI, WATCH, FLEX, AUDIT, SMTP test y generación del dashboard.
- GitHub Pages: dashboard estático.
- Dashboard → GitHub Issues: puente sin tokens para Guardar/Eliminar/WATCH/FLEX/AUDIT. El workflow sólo acepta comandos creados por el owner del repositorio.
- SerpApi: conserva el budget guard existente.

## 1. Crear el repositorio

Crear en GitHub un repositorio **público** llamado:

`cjacobs32/flight-sentinel-miami`

No inicializarlo con README, `.gitignore` ni licencia para que la primera migración sea directa.

## 2. Aplicar V2.9 sobre el clon local actual

Descomprimir el ZIP de migración sobre la raíz del proyecto actual. Luego ejecutar:

```powershell
.\PREPARE_GITHUB_MIGRATION.ps1
git status
py -3.14 -m pytest -q
```

El script elimina únicamente los archivos operativos obsoletos de GitLab; no toca `monitors.yaml`, `.env`, bases locales ni `flight-state`.

La migración cloud no se dispara por `push`; esto evita crear un estado vacío durante el primer traslado. El despliegue inicial se hace manualmente con `render` después de copiar `flight-state`.

Luego crear el commit de migración:

```powershell
git add .
git commit -m "Migrate Flight Sentinel cloud to GitHub Actions"
```

## 3. Conservar GitLab como remoto de respaldo y migrar ambas ramas

Desde el clon local que hoy apunta a GitLab:

```powershell
git fetch origin main flight-state
git remote rename origin gitlab
git remote add origin https://github.com/cjacobs32/flight-sentinel-miami.git
git push -u origin main
git push origin refs/remotes/gitlab/flight-state:refs/heads/flight-state
```

No eliminar el remoto `gitlab` hasta validar completamente GitHub.

## 4. Configurar GitHub Pages

En GitHub:

`Settings → Pages → Build and deployment → Source → GitHub Actions`

El workflow `.github/workflows/flight-sentinel.yml` genera y despliega `public/index.html`.

## 5. Permisos de Actions

En:

`Settings → Actions → General → Workflow permissions`

habilitar **Read and write permissions** para que `GITHUB_TOKEN` pueda:

- persistir `flight-state`;
- guardar `monitors.yaml`;
- comentar/cerrar los issues de comando.

No se necesita PAT.

## 6. Secrets y variables

### Secrets

`Settings → Secrets and variables → Actions → Secrets`

- `SERPAPI_API_KEY`
- `SMTP_PASSWORD`

### Variables

`Settings → Secrets and variables → Actions → Variables`

- `SMTP_HOST=smtp.gmail.com`
- `SMTP_PORT=587`
- `SMTP_USE_TLS=true`
- `SMTP_USER=<gmail>`
- `EMAIL_FROM=<gmail>`
- `EMAIL_TO=<destinatario>`
- `EMAIL_SUBJECT_PREFIX=[Flight Sentinel]`

No guardar secretos en el repositorio.

## 7. Primera ejecución

Ir a:

`Actions → Flight Sentinel Cloud → Run workflow`

Primero ejecutar `render`. No consume SerpApi y debe publicar el estado existente recuperado desde `flight-state`.

Después ejecutar `smtp_test`. Tampoco consume SerpApi.

Una vez validados Pages y SMTP, ejecutar `watch`. AUDIT sólo cuando haga falta reconstruir la consulta completa.

## 8. Schedules incorporados

El workflow ya define:

- WATCH: 00:17, 06:17, 12:17 y 18:17 en `America/Argentina/Buenos_Aires`.
- AUDIT: lunes y jueves 03:40 en `America/Argentina/Buenos_Aires`.

## 9. Acciones desde el dashboard

Pages es estático y no almacena credenciales. Los botones crean un issue prellenado con un bloque como:

```text
FLIGHT_SENTINEL_COMMAND_V1
FLIGHT_ACTION=flex
FLIGHT_MONITOR_ID=aruba-enero
```

El usuario confirma `Create issue`. El workflow sólo procesa el comando si el autor del issue coincide con `github.repository_owner`; al finalizar lo comenta y cierra.

## 10. Criterio de validación

La migración se considera completa cuando:

- CI está verde.
- `render` despliega Pages.
- Pages recupera monitores e histórico desde `flight-state`.
- `smtp_test` informa el resultado real de SMTP.
- WATCH funciona sin reemplazar la consulta completa.
- AUDIT conserva el guardia SerpApi y las reglas EZE/AEP → destino configurable, 0/1 escala, conexión ≤2 h.
- FLEX construye la grilla con el sensor gratuito y sólo verifica un conjunto acotado de mejores fechas.
- Guardar/Editar/Eliminar desde el dashboard actualiza `monitors.yaml` mediante GitHub Actions.

## Script opcional de migración

Después de commitear V2.9 podés ejecutar desde PowerShell:

```powershell
.\MIGRATE_GITLAB_TO_GITHUB.ps1
```

El script se detiene si hay cambios sin commit, conserva GitLab como remote `gitlab`, comprueba acceso al repo GitHub y copia `main` + `flight-state`.

## 11. Destinos y fechas flexibles (V2.9)

Cada monitor guarda `destination` y `flex_days`. `flex_days=0` usa sólo fechas exactas; 1..3 habilita una ventana independiente alrededor de ida y vuelta. El botón **Explorar fechas flexibles** usa Fli/SearchDates primero y SerpApi sólo para verificar las mejores combinaciones, respetando el guardia mensual.

## V2.9.1 — paridad Despegar restaurada

- El dashboard vuelve a mostrar **Ver en Despegar** con URL dinámica BUE → destino para las fechas/pasajeros del monitor.
- **Auditar Despegar gratis** ejecuta el auditor independiente con Playwright usando la última corrida Google ya guardada; no dispara una búsqueda SerpApi nueva.
- El resultado se conserva en `flight-state` como `last_market_audit` y se muestra en **Detalles y cobertura**.
- Si Despegar bloquea automatización/CAPTCHA, el estado queda informado y la corrida Google no se invalida.
