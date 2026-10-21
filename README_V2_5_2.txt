Flight Sentinel V2.5.2 - WATCH Fix

Corrige:
- Normalización de Fli 0.9.0: usa Enum.name para códigos IATA de aeropuerto y aerolínea.
- Evita enviar nombres largos de aeropuertos a SerpApi.
- Evita guardar IDs numéricos de aerolíneas (130, 154, etc.) como si fueran códigos IATA.
- Descarta automáticamente el snapshot WATCH defectuoso generado por V2.5.1 y crea un baseline limpio.
- Mantiene flights==0.9.0.
- Actualiza los tests.

Validación:
282 tests OK, 10 skipped, 0 fallos.

Aplicación:
1. Cerrar cualquier dashboard local.
2. Descomprimir este ZIP sobre la raíz del proyecto aceptando reemplazar.
3. Ejecutar:
   git add .
   git commit -m "Fix Fli 0.9 WATCH normalization"
   git push origin main
4. Ejecutar nuevamente la pipeline WATCH manual.
