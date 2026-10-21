# Flight Sentinel V2 — Buenos Aires → destinos configurables


## V2.9 — Destino configurable + fechas flexibles

V2.9 elimina el destino hardcodeado en Miami y agrega exploración de fechas al estilo de una grilla de tarifas, sin abandonar el requisito de costo **$0**.

- Origen protegido: **EZE + AEP**.
- Destino: cualquier código IATA de 3 letras soportado por Fli/Google Flights; el dashboard permite buscar por código o nombre. Ejemplo: **Aruba = AUA**.
- Cada monitor conserva una pareja de **fechas base** y puede usar `Exactas`, `±1`, `±2` o `±3 días`.
- La flexibilidad modifica ida y vuelta **de manera independiente**: puede detectar, por ejemplo, ida +1 y vuelta +2.
- Nueva pestaña **Fechas flexibles** con matriz ida/vuelta, mejores combinaciones y ahorro potencial.
- La matriz se construye primero con **Fli/SearchDates** como sensor gratuito. Los precios de sensor no se presentan como reservables.
- Sólo las mejores combinaciones se verifican con SerpApi/Booking Options detrás del guardia mensual. Por defecto se verifican hasta 2.
- Explorar fechas flexibles **no cambia automáticamente** las fechas del monitor ni su histórico.
- `AUDIT` sigue siendo la consulta completa y autoritativa para las fechas base.

Acción cloud nueva: `flex`. En GitHub Pages aparece como **⇆ Explorar ±N días**.

> **UI objetivo de V2.9:** `cloud_dashboard.py` + GitHub Pages. `dashboard.py` se conserva sólo como interfaz local histórica/legacy y no participa del despliegue GitHub ni de la nueva experiencia de destinos/flexibilidad.

## V2.8 — GitHub Actions + GitHub Pages

V2.8 migra la operación cloud completa desde GitLab a GitHub manteniendo la política de **costo $0** y la arquitectura **WATCH → VERIFY → AUDIT**.

- **GitHub Actions** ejecuta WATCH, AUDIT, SMTP test y render del dashboard.
- **GitHub Pages** publica el dashboard estático.
- La rama **`flight-state`** conserva `state.json` e histórico de manera durable.
- `monitors.yaml` permanece en `main`.
- Los botones del dashboard no contienen tokens: abren un **GitHub Issue prellenado**; sólo los issues creados por el owner del repositorio pueden disparar comandos.
- `workflow_dispatch` queda disponible para ejecuciones manuales desde Actions.
- Schedules: WATCH 4 veces por día y AUDIT lunes/jueves, en `America/Argentina/Buenos_Aires`.
- Secrets soportados: `SERPAPI_API_KEY` y `SMTP_PASSWORD`; el resto del SMTP se configura como repository variables.
- El workflow usa únicamente `GITHUB_TOKEN` efímero para actualizar configuración/estado. No requiere PAT.

Para mantener **$0 estricto con GitHub Free**, el diseño está preparado para un repositorio **público**: Pages y los runners estándar de Actions se utilizan sin contratar servicios pagos. Ver `GITHUB_ACTIONS_V2_8.md`.

### Acciones cloud

- `render`: republica Pages sin consultar vuelos.
- `watch`: sensor Fli frecuente; no equivale a cobertura exhaustiva.
- `audit`: consulta completa Google Flights/SerpApi con guardia de cuota.
- `flex`: explora fechas cercanas con Fli/SearchDates y verifica selectivamente las mejores.
- `smtp_test`: prueba SMTP sin consumir SerpApi.
- `save_monitor` / `delete_monitor`: comandos emitidos desde el dashboard mediante GitHub Issues.

### Persistencia

`main` contiene código y `monitors.yaml`. `flight-state` contiene únicamente el estado cloud (`state.json`). Al migrar desde GitLab conviene copiar **ambas ramas** para conservar configuración e histórico.

## V2.4.4 — Precio reservable verificado y opciones de compra

- Se separa **precio observado en búsqueda** de **precio reservable verificado** para el itinerario exacto.
- Booking Options se aprovecha en la misma consulta `selected_flights_json` usada para equipaje/deep-link; no agrega otra llamada cuando la oferta ya se está enriqueciendo.
- Cada oferta enriquecida guarda:
  - vendedor más barato + precio reservable;
  - mejor opción directa con aerolínea cuando Google la informa;
  - hasta 12 opciones de reserva estructuradas;
  - precio observado original para trazabilidad.
- La card muestra el precio reservable como principal cuando está verificado y conserva el observado como contexto.
- La etiqueta `MEJOR CON ...` pasa a `MEJOR ITINERARIO DE ...` para no confundir aerolínea operadora con vendedor.
- El histórico y las alertas siguen usando el precio observado de búsqueda, porque está disponible para todas las ofertas y es comparable entre corridas.
- Si un histórico anterior ya tiene deep-link/equipaje pero no Booking Options, el primer clic en **Ver oferta en Google** verifica y persiste las opciones de reserva antes de redirigir.
- Equipaje facturado `PAID` sin importe pasa a mostrarse como **Con cargo · importe no informado**, no como dato desconocido.
- El dashboard muestra el mejor precio reservable verificado entre las ofertas enriquecidas sin presentarlo como cobertura exhaustiva de mercado.


## V2.4.1 — Pulido UX/UI

- Ofertas más compactas: precio + vuelo + equipaje + acción en el primer nivel.
- CTA verde para abrir la oferta en Google Flights; detalles bajo demanda.
- Cambio vs. la corrida anterior por combinación comparable de aerolíneas.
- Badges estandarizados: verde éxito, azul información, naranja atención, rojo crítico.
- Timeline del histórico resumido a 5 eventos con el resto plegable.
- Sin nuevas llamadas API, servicios ni costos.

## V2.4 — Rediseño UX/UI total

La V2.4 reorganiza el dashboard alrededor de tres tareas: **comparar ofertas, entender la cobertura y revisar el histórico**. El motor de búsqueda no cambia.

Principales mejoras:

- pestañas **Comparación / Detalles y cobertura / Histórico**;
- hero con mejor precio, promedio por pasajero, cambio vs. corrida anterior y cobertura;
- ofertas en **cards responsive** en vez de tablas horizontales comprimidas;
- filtros por aerolínea y tipo de vuelo, y orden por precio/duración;
- itinerario y equipaje completos plegados bajo demanda;
- vista técnica separada para cobertura Google, mercado, SerpApi y catálogo IATA;
- histórico con métricas, gráfico, filtros de período y timeline de cambios observados;
- descarga CSV;
- formulario de edición reorganizado y mobile-first;
- fechas visibles e inputs siempre en **DD/MM/AAAA**;
- sin nuevas APIs, dependencias pagas ni costos.

El rediseño reutiliza la base de datos y configuración existentes; no requiere una corrida nueva para visualizar la última información guardada.


## Hotfix V2.3.2

- Bloqueo de concurrencia por monitor: una doble pulsación o dos pestañas no pueden iniciar dos consultas SerpApi simultáneas.
- Las acciones largas usan POST/Redirect/GET y toleran que el navegador cierre la conexión sin convertir una corrida válida en HTTP 400.
- El auditor Despegar queda manual por defecto (`MARKET_AUDIT_AUTO=false`) porque el sitio puede activar protección anti-bot.
- Si Despegar devuelve 0 tarjetas parseadas, el estado es **NO VERIFICADA / INCONCLUSA**; nunca se interpreta como 0 vuelos.
- Se vuelve a comprobar el bloqueo anti-bot después de la espera y del scroll, porque el challenge puede aparecer tarde.
- El contador de aerolíneas Google incluye todas las aerolíneas presentes en las ofertas, también carriers secundarios.
- Se mantiene DD/MM/AAAA en toda entrada/salida visible y costo cero como restricción de diseño.


Monitor de precios de vuelos ida y vuelta desde **EZE/AEP hacia un destino configurable por IATA**, evolucionado a partir del monitor Google Flights original.

## Regla funcional fija

Flight Sentinel V2 sólo considera una oferta **válida** cuando ida y vuelta cumplen, por separado:

- destino **configurable por código IATA** (por ejemplo `MIA`, `AUA`, `PUJ`);
- origen **EZE o AEP**;
- **vuelo directo**, o como máximo **1 escala**;
- si existe una escala, su duración debe ser **≤ 2 horas (120 minutos)**;
- una oferta con ida válida pero vuelta sin detalle **no se guarda, no entra al ranking y no puede disparar una alerta**.

Los límites de escalas están validados en configuración y no pueden relajarse accidentalmente desde `config.yaml`, `.env` o CLI. El destino sí es editable por monitor. Podés hacer las escalas más estrictas, por ejemplo `MAX_STOPS=0` para sólo directos.

## Qué cambió respecto del ZIP anterior

- Ruta por defecto cambiada de Punta Cana a **Miami**.
- Filtro estricto `MAX_STOPS=1` + `MAX_LAYOVER_HOURS=2.0`.
- SerpApi recibe además su filtro nativo **1 escala o menos** y luego el programa vuelve a validar localmente cada itinerario.
- Se eliminó el fallback que fabricaba una vuelta sin detalle cuando Google no devolvía `departure_token`/retorno.
- EZE y AEP pueden consultarse juntos en SerpApi (`EZE,AEP`) para reducir búsquedas duplicadas.
- Nuevo concepto de **monitor**: cada combinación de fechas tiene ID, pasajeros e histórico independiente.
- Dashboard web local para crear/editar monitores, ejecutar una corrida y ver histórico/gráfico.
- Base SQLite migrable: agrega `monitor_id` y evita mezclar baselines entre ventanas de fechas distintas.
- `.gitignore` incorporado. La distribución V2 no incluye `.env`, base, logs ni históricos del ZIP original.

## Fuente principal: Google Flights vía SerpApi

El provider recomendado es `serpapi`. La integración usa `deep_search=true`, `sort_by=price` y el filtro nativo de escalas. La validación de la escala de 120 minutos se realiza en nuestro código sobre los segmentos devueltos.

Con `SERPAPI_COMBINE_ORIGINS=true`, EZE y AEP se envían en una sola búsqueda inicial. V2.2 recomienda `SERPAPI_MAX_OUTBOUND_TOKENS=15` como **techo**, no como consumo fijo: si Google devuelve 9 salidas con token, se exploran 9 y la corrida registra 10 consultas lógicas (1 discovery + 9 vueltas). La pantalla indica si la cobertura fue COMPLETA o PARCIAL.

## Instalación rápida en Windows

1. Descomprimir el ZIP.
2. Ejecutar `SETUP_WINDOWS.bat`.
3. Completar `.env` con `SERPAPI_API_KEY` y, si querés email, SMTP.
4. Crear o editar `monitors.yaml` (también se puede hacer desde la web).
5. Ejecutar `START_DASHBOARD.bat`.
6. Abrir `http://127.0.0.1:8766`.

El dashboard no usa Flask ni otro framework web: funciona con la biblioteca estándar de Python.

## Reutilizar las credenciales del monitor anterior

El ZIP V2 **no incluye tu `.env` real** para evitar volver a empaquetar secretos.

Podés migrarlo sin copiar claves a mano:

```bat
python migrate_env_v1.py "C:\RUTA\AL\MONITOR_ANTERIOR\.env"
```

El script conserva las credenciales localmente y fuerza la nueva política:

```text
ORIGINS=EZE,AEP
DESTINATION=MIA
MAX_STOPS=1
MAX_LAYOVER_HOURS=2
SERPAPI_COMBINE_ORIGINS=true
```

No imprime los secretos en pantalla.

## Crear varios monitores

`monitors.yaml` contiene una entrada por ventana de fechas:

```yaml
monitors:
  - id: aruba-enero
    name: "Aruba - enero"
    enabled: true
    destination: AUA
    date_outbound: "2027-01-01"
    date_return: "2027-01-15"
    flex_days: 3
    adults: 2
    children_ages: [8]
    cabin_class: ECONOMY
    currency: USD
```

Las fechas del ejemplo son sólo ilustrativas. Reemplazalas por las fechas reales.

Comandos:

```bat
python main.py --monitor miami-julio
python main.py --all-monitors
python main.py --dashboard
```

## Dashboard

Desde la interfaz podés:

- crear varias fechas de viaje;
- pausar/activar monitores;
- ejecutar un monitor manualmente;
- ver el último precio válido;
- ver cantidad de ofertas válidas;
- revisar el histórico por monitor;
- visualizar la evolución del mejor precio.

Los campos de ruta y escalas aparecen bloqueados porque son parte de la política funcional de esta versión.

## Configuración de SerpApi

En `.env`:

```text
PRICE_PROVIDER=serpapi
SERPAPI_API_KEY=tu_clave
SERPAPI_MAX_OUTBOUND_TOKENS=15
SERPAPI_COMBINE_ORIGINS=true
```

`SERPAPI_MAX_OUTBOUND_TOKENS` es el máximo de salidas de ida a explorar. No obliga a consumir esa cantidad. La cobertura real queda registrada en cada snapshot para distinguir una corrida completa de una parcial.

## Alertas

La lógica existente de alertas se conserva:

```text
ALERT_ABSOLUTE_DROP=50
ALERT_PERCENT_DROP=5
ALERT_MODE=either
BASELINE_MODE=min
ALERT_MIN_HOURS_BETWEEN=6
```

El baseline ahora está aislado por `monitor_id`. Una fecha de julio no contamina el mínimo histórico de otra fecha de agosto.

## Seguridad

Nunca subir al repositorio:

- `.env`;
- `*.db`;
- logs;
- CSV/JSON generados;
- `.venv/`.

El `.gitignore` ya contempla estos archivos.

## Tests

```bat
python -m pytest -q
```

La suite incluye regresión de los providers anteriores y tests específicos de V2 para:

- directo;
- escala exactamente de 120 minutos;
- rechazo a 121 minutos;
- filtro nativo de SerpApi;
- ausencia de fallback de vuelta incompleta;
- aislamiento de histórico por monitor;
- política fija Miami.

## V2.1 — detalle de ofertas y fuente visible

La pantalla de detalle del monitor ahora muestra las ofertas válidas de la última corrida como fichas completas:

- aerolínea(s) y códigos IATA;
- precio total del grupo;
- promedio por pasajero (`total / cantidad de pasajeros`);
- ida y vuelta por separado;
- vuelo(s) y números de vuelo;
- horarios de salida y llegada;
- duración del tramo;
- `DIRECTO` o detalle de la única escala permitida;
- aeropuerto y duración exacta de la escala;
- enlaces a Google Flights / aerolínea cuando están disponibles;
- histórico con fuente, aerolínea y tipo de conexión.

El promedio por pasajero es una referencia matemática. Si hay adultos y menores, la tarifa individual real de cada pasajero puede ser diferente.

### Simulación vs. precio real

El dashboard muestra siempre la fuente y el modo de la corrida. `mock`, Duffel Test y Amadeus Test aparecen explícitamente como simulación/sandbox y no como precios reales.

A partir de V2.1 el proveedor por defecto es `serpapi`. Si no hay API key, la corrida falla de forma visible en lugar de generar silenciosamente datos mock.

Para usar Google Flights real:

```text
PRICE_PROVIDER=serpapi
SERPAPI_API_KEY=tu_clave
```

Si venís del monitor Google Flights anterior, podés reutilizar tu `.env` con `migrate_env_v1.py`.

### Filtro adicional en Google Flights

Además del filtro nativo de máximo una escala, V2.1 envía a SerpApi el límite de duración de conexión de hasta 120 minutos y vuelve a validarlo localmente antes de guardar cualquier oferta.


## V2.2 — resumen inteligente, cobertura y fechas DD/MM/AAAA

La V2.2 cambia la pantalla de resultados para evitar una lista larga de vuelos casi iguales. Por defecto muestra:

- el **mejor precio general**;
- la **mejor alternativa de cada aerolínea** encontrada;
- la diferencia absoluta y porcentual contra el mejor precio;
- un botón **Ver todas las alternativas (N)** para acceder a todas las combinaciones válidas.

La corrida guarda además métricas de cobertura:

- salidas de ida detectadas por Google Flights;
- salidas efectivamente exploradas mediante `departure_token`;
- cobertura **COMPLETA** o **PARCIAL**;
- cantidad de consultas lógicas SerpApi utilizadas.

Estas métricas también aparecen en el histórico. Las corridas anteriores a V2.2 quedan como **Cobertura no registrada**, porque ese dato no existía todavía. La migración de SQLite es automática y no requiere borrar la base.

### Formato de fechas

Toda fecha visible o editable en la interfaz usa exclusivamente:

```text
DD/MM/AAAA
```

Ejemplo: `10/12/2026`.

Internamente, las APIs, SQLite y `monitors.yaml` siguen usando ISO `AAAA-MM-DD` para mantener ordenamiento y compatibilidad técnica. Esa conversión es automática cuando se usa el dashboard.

El CLI también acepta fechas en formato visible:

```bat
python main.py --depart-date 10/12/2026 --return-date 17/12/2026
```

## V2.3 — Google expandido + auditor independiente de mercado

V2.3 corrige una interpretación importante de versiones anteriores: **"Cobertura Google COMPLETA" no significa "mercado completo"**.

La corrida ahora tiene dos controles separados:

1. **Cobertura Google Flights**
   - `deep_search=true`;
   - `show_hidden=true` (incluye los resultados detrás de "Ver más vuelos");
   - localización `gl=ar`, `hl=es`;
   - `EZE,AEP → MIA`;
   - máximo 1 escala;
   - máximo 120 minutos por escala;
   - se exploran todos los `departure_token` hasta `SERPAPI_MAX_OUTBOUND_TOKENS`.

2. **Cobertura de mercado independiente**
   - abre Despegar con **Playwright + Chrome local**;
   - no requiere API key ni servicio pago;
   - identifica aerolíneas y resultados visibles;
   - compara las aerolíneas elegibles observadas contra Google;
   - los vuelos directos son evidencia fuerte;
   - una opción de 1 escala sólo se declara discrepancia si la duración de la conexión puede verificarse como `≤ 120 min`; si no, queda **PARCIAL / por verificar**.

Estados de mercado:

- `CONSISTENT`: no se detectaron diferencias relevantes entre las dos fuentes observadas. **No significa mercado exhaustivo**.
- `DISCREPANCY`: Despegar encontró al menos una aerolínea con alternativa elegible que Google no mostró.
- `PARTIAL`: hay resultados de 1 escala cuya duración no pudo verificarse automáticamente o la evidencia es incompleta.
- `AUDIT_FAILED`: Despegar no pudo auditarse (bloqueo, CAPTCHA, timeout, Playwright ausente, etc.).

### Auditoría sin gastar SerpApi

En el dashboard aparece el botón:

```text
Auditar Despegar gratis
```

Ese botón vuelve a consultar Despegar y reconcilia contra la **última corrida Google ya guardada**, por lo que no consume una nueva consulta SerpApi.

### Alertas conservadoras

Por defecto:

```text
MARKET_ALERT_REQUIRE_VALIDATION=true
```

Si el precio baja pero la cobertura de mercado está en `DISCREPANCY`, `PARTIAL` o `AUDIT_FAILED`, Flight Sentinel guarda el precio en el histórico pero **no lo anuncia como mejor precio de mercado**.

### Costo

La arquitectura V2.3 mantiene la política de costo cero:

- Despegar: Playwright local, sin API paga;
- navegador: usa el Chrome ya instalado en Windows;
- SQLite/dashboard: local;
- SerpApi: sólo el free tier configurado por el usuario; no se agrega ningún proveedor pago ni fallback con cobro.

Si el free tier de SerpApi se agota, la aplicación falla de forma visible; no cambia automáticamente a un servicio que pueda generar cargos.

## V2.3.1 — hotfix de ejecución, fechas y bloqueo Despegar

Esta revisión corrige tres problemas observados en una corrida real:

- el formulario de fechas renderizaba incorrectamente el patrón HTML por una interacción con `f-string`; ahora `10/11/2027` y cualquier fecha válida `DD/MM/AAAA` son aceptadas correctamente;
- las fechas se autoformatean mientras se escriben (`DD/MM/AAAA`) y el servidor sigue validando calendario real antes de guardar;
- las acciones largas muestran overlay, spinner y segundos transcurridos para que quede visible que la búsqueda sigue en proceso.

También se corrigió un error de construcción de `DespegarAudit` que podía abortar la corrida luego de haber obtenido correctamente las ofertas de Google. A partir de V2.3.1, **el auditor secundario nunca puede hacer perder una corrida Google real**.

Despegar puede mostrar protección anti-automatización (`Verification Required`, `Access is temporarily restricted`, etc.). Flight Sentinel no intenta evadir CAPTCHA ni mecanismos anti-bot. En ese caso:

- Google Flights se guarda normalmente;
- la cobertura Google conserva su diagnóstico;
- la cobertura de mercado queda `NO VERIFICADA · BLOQUEADA`;
- se conserva evidencia del bloqueo cuando es posible;
- una alerta que requiera validación de mercado sigue retenida de forma conservadora.

## V2.3.3 — aerolíneas y equipaje

La interfaz usa el nombre de aerolínea que devuelve Google Flights/SerpApi como fuente primaria. Para códigos sin nombre se incluye `data/airlines_iata.json` como catálogo de respaldo, derivado de OpenFlights (ver `data/OPENFLIGHTS_NOTICE.txt`). El catálogo local no reemplaza al proveedor ni se considera una lista oficial IATA en tiempo real.

Para las ofertas destacadas, Flight Sentinel consulta Google Flights Booking Options y muestra por ida/vuelta:

- artículo personal;
- carry-on / equipaje de cabina;
- equipaje despachado / bodega;
- costo de la primera valija cuando Google lo informa.

Lo que la tarifa no informa se muestra como **No informado**. No se deduce equipaje por reglas genéricas de la aerolínea, porque puede variar por tarifa, ruta y vendedor.

Configuración:

```env
SERPAPI_BAGGAGE_ENRICH=true
SERPAPI_BAGGAGE_MAX_OFFERS=6
```

Cada Booking Option consultada puede consumir una búsqueda adicional del free tier de SerpApi. Por eso el enriquecimiento se limita a ofertas representativas y la interfaz incluye esas llamadas dentro del contador total de consultas SerpApi.

## V2.3.4 — hotfix Booking Options y ahorro de cuota

Corrige el error `SerpApi HTTP 400: Missing departure_id parameter` observado al intentar verificar equipaje. La verificación ya no depende del `booking_token`: Flight Sentinel fija el itinerario exacto mediante `selected_flights_json`, enviando cada segmento con número de vuelo, aeropuerto de salida/llegada y fecha. Es el mecanismo documentado por SerpApi para obtener Booking Options de un itinerario específico.

Además, cuando una misma oferta (mismo itinerario, horarios y precio) ya tiene equipaje `VERIFIED` en SQLite, la información se reutiliza y **no consume otra búsqueda SerpApi**. Si el precio cambia, se vuelve a verificar para no asumir que la franquicia de una tarifa anterior sigue vigente.

La política de costo continúa siendo cero: no se incorpora ningún proveedor pago ni fallback con cobro.

## V2.3.5 — interfaz compacta

La vista de resultados prioriza comparación rápida. Muestra aerolínea, precio total y por pasajero, directo/escala y equipaje sin desplegar todo el itinerario. Cada oferta puede abrirse individualmente para ver segmentos, horarios y detalle de equipaje. El histórico también queda plegado para reducir la longitud de la página.


## V2.4.2 — enlaces Google Flights

Los botones de Google Flights usan deep-links estructurados (`tfs`) y ya no dependen de búsquedas en lenguaje natural. Las corridas nuevas priorizan `search_metadata.google_flights_url` de SerpApi; los snapshots antiguos se migran en memoria al abrirlos, sin consumir una nueva búsqueda.

## V2.4.3 — apertura de la oferta exacta en Google Flights

Los CTA de cada oferta distinguen ahora entre una búsqueda general y el itinerario exacto.
Flight Sentinel usa `selected_flights_json` (vuelo por vuelo, ida y vuelta) para resolver el deep-link de Google Flights de la oferta elegida.

- Corridas nuevas: el link exacto se captura durante la misma consulta de Booking Options usada para equipaje; no agrega consultas.
- Snapshots históricos: el primer clic en una oferta sin link exacto hace una resolución puntual, persiste el resultado y los clics siguientes reutilizan el link.
- Si no puede fijarse el itinerario, la app no abre silenciosamente una búsqueda general: informa el problema y ofrece esa búsqueda como fallback explícito.

---

## V2.7 — Cloud parity con el dashboard local

V2.7 corrige la regresión de la primera migración cloud. **WATCH/Fli ya no es la fuente principal de comparación**: queda como sensor gratuito frecuente. La acción **Actualizar precios** dispara la consulta completa de Google Flights/SerpApi y la UI usa esa colección por defecto.

Se recupera la línea visual de V2.4.4: cards representativas por aerolínea, todas las variantes bajo demanda, precio observado vs. reservable, delta, vuelo/carry-on/bodega, duración, equipaje, Booking Options, enlaces exactos, cobertura e histórico.

La consulta completa verifica hasta `AUDIT_VERIFY_MAX_OFFERS=4` ofertas representativas por defecto (mejor + nuevas aerolíneas), siempre detrás del guardia de cuota. WATCH continúa siendo el mecanismo frecuente $0 y VERIFY sigue ejecutándose sólo cuando corresponde.

Ver `GITLAB_CLOUD_FULL_V2_7.md` para el diseño completo.
