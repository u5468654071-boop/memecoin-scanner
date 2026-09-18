# Revisión y validación de la v0.4

Base revisada: commit `9eda5fa` del repositorio `u5468654071-boop/memecoin-scanner`.

## Fallos corregidos

1. **El filtro admitía verificaciones ausentes.** Un error/404 de RugCheck dejaba `has_danger_flag=None` y `lp_locked_pct=None`; el filtro original no rechazaba esos estados. Ahora exige campos completos y válidos.
2. **El precio podía pertenecer a otro token.** Se elegía el par de mayor liquidez sin validar `baseToken` ni cadena. Ahora ambos se comprueban y el seguimiento conserva la identidad del pool original.
3. **La comunidad no estaba verificada.** Tener enlaces y una web que responde daba puntos para ordenar candidatos; una web se consultaba hasta dos veces. Se eliminan esas llamadas y sus puntos; los enlaces son solo metadatos declarados.
4. **Las webs arbitrarias podían provocar solicitudes internas.** Las peticiones ahora se limitan a las APIs conocidas, sin redirecciones. No se abren URLs enviadas por promotores.
5. **La migración CSV podía volver a mezclar columnas.** Si ya existía el archivo de respaldo de ese día, el código original seguía escribiendo el nuevo esquema en el antiguo. Ahora cada migración tiene nombre único; además se usa un archivo v0.4 separado por defecto y SQLite como historial principal.
6. **No se medían resultados posteriores.** Ahora se registran observaciones a 1/6/24 horas, con denominadores, cobertura, motivos de ausencia y políticas separadas. La evaluación debe ejecutarse durante las ventanas; no se ha instalado un planificador.

## Pruebas realizadas

- **40 tests automáticos aprobados**, sin red, en Python 3.9.6.
- Compilación de `memecoin_scanner.py` y `tracking.py` y comprobación de `--help`.
- `git diff --check`, sin errores de espacios.
- Escaneo real de **6 tokens en Solana**: mercado y resumen RugCheck disponibles para los seis; **0 seleccionados** con la política predeterminada. No se han relajado los criterios para producir candidatos artificialmente.
- Consulta del reporte local de seguimiento. Los plazos de 1/6/24h no habían terminado durante la entrega; **no hay resultados prospectivos de rentabilidad**. Las transiciones temporales se han probado con reloj y respuestas simulados.
- Se añade CI para Python 3.9/3.12/3.13, pero el workflow aún no se ha ejecutado en GitHub.

Las respuestas reales y la salida de los tests se entregan como evidencia en la carpeta `validacion` junto al ZIP. Son datos de un instante, no recomendaciones vigentes.

## Qué no demuestra esta revisión

No demuestra que el ranking gane dinero ni que un token sea seguro. Los pesos y umbrales son heurísticos sin calibración estadística. El bloqueo LP es el que reporta RugCheck para el token; no valida de forma independiente el pool elegido. No hay prueba de venta, análisis de insiders/bundles, identificación de usuarios reales ni valoración de costes de ejecución. Una API puede entregar datos retrasados.

El siguiente paso para validar utilidad es recoger observaciones prospectivas con una política fija, suficiente cobertura y tokens distintos; después comparar seleccionados y descartados incluyendo los casos no observables. Ajustar reglas a la misma muestra y presentar esa muestra como prueba de rendimiento produciría sobreajuste.

## Estado de entrega

Cambios preparados y probados localmente en la rama `improve/scanner-validation-and-tracking`. No se han enviado commits ni creado PRs en GitHub. El ZIP contiene el proyecto completo actualizado; el parche aplica sobre el commit base indicado. Los históricos originales se conservan.
