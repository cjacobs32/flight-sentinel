Flight Sentinel V2.7.2 — Dashboard Consistency Fix

Corrige inconsistencias detectadas en la primera consulta completa real de V2.7.1:

1. El precio principal usa el MENOR precio reservable verificado.
   Ejemplo observado: AR USD 3.206 debía prevalecer sobre AA USD 3.630.

2. El enlace principal de Google Flights apunta al mismo itinerario que el precio principal.

3. Si precio observado == precio reservable, la card sigue mostrando VERIFICADO.
   Antes AA USD 3.630 podía aparecer como “todavía no verificado” aun con Booking Options confirmadas.

4. El histórico usa el menor precio verificado de cada corrida cuando existe.

5. En itinerarios con más de una aerolínea el badge usa “MEJOR OPCIÓN CON …”
   para no atribuir erróneamente todo el itinerario a una sola compañía.

Validación:
- 301 tests passed
- 10 skipped
- 0 failed
- JavaScript validado con node --check

Aplicación:
1. Descomprimir sobre la raíz del proyecto aceptando reemplazar.
2. Ejecutar:
   git add .
   git commit -m "Fix dashboard verified price consistency"
   git push origin main
3. El push valida tests. Luego ejecutar una pipeline WATCH o refrescar Pages tras el pipeline.
