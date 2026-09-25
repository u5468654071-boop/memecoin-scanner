# Validación v0.11.0 — riesgo de posición y evaluación

25 de septiembre de 2026. Base pública y desplegada: `fd543ae705a94e3d408530c8fdd6494e5f85a134`, v0.10.0. Todo continúa en simulación, sin wallet, firmas ni envío de operaciones.

## Evidencia que motivó la revisión

Lectura coherente de las tablas del VPS a las 21:29:35 UTC:

| Perfil | Cierres acumulados | Monedas únicas | Resultado ficticio, USDC | Cierres por «candidato invalidado» |
| --- | ---: | ---: | ---: | ---: |
| Conservador | 0 | 0 | 0 | 0 |
| Equilibrado | 30 | 13 | −19,964489 | 21 |
| Agresivo | 40 | 20 | −3,243021 | 27 |

Son acumulados desde el inicio, no únicamente la semana natural. Hay 24 monedas únicas entre perfiles, 8 entradas v0.9 y 62 v0.10. El saldo ficticio es 976,792490 USDC, sin posiciones abiertas en esa lectura. No se ha demostrado una ventaja positiva.

La regla anterior cerraba ante cualquier `quality_pass=false`, mezclando criterios de compra y mantenimiento. En 16 de las 48 invalidaciones, la primera observación invalidante tiene únicamente condiciones que la nueva clasificación trata como exclusivas de entrada. Es un diagnóstico de reglas sobre datos guardados: **no** es una simulación de las salidas alternativas, ni permite afirmar cuánto habría cambiado el beneficio. Otras invalidaciones tienen deterioro orgánico, LP, caída de precio o volumen anómalo y siguen requiriendo salida.

El estudio de cotizaciones confirmado v0.10 a cuatro horas tiene cinco monedas observadas: media +613,54%, mediana −57,83%. Una sola moneda explica todos sus retornos positivos; sin ella, las cuatro restantes promedian −63,25%. Los importes brutos guardados reproducen los retornos extremos, por lo que no se han borrado ni presentado como un error de decimales. Ese estudio tampoco es el rendimiento de las carteras.

## Cambios y garantías verificables

- `position_risk` separa el mantenimiento de la elegibilidad para entrar. Conserva riesgos críticos y datos ausentes; no vende únicamente por edad conocida fuera de ventana, subida por encima del máximo de entrada, reconstrucción de confirmación o falta de una nueva compra cotizada.
- La evidencia de riesgo caduca según el límite de datos del perfil. Se verifican tanto la observación como las fechas originales de mercado y actividad orgánica, también después de una consulta de venta lenta.
- El coste neto inicial se verifica sobre la cantidad exacta que queda tras el slippage, con ambos costes fijos. No basta una primera cotización de ida y vuelta favorable si el mercado empeora después.
- `exit_evidence` conserva causa, observación, perfil, fechas y disparadores simultáneos. Una ruta ausente mantiene capital comprometido y causa de salida pendiente. La migración es aditiva, serializada y no reinicia saldos.
- Nuevas cohortes de primera confirmación por perfil, sin reclasificar muestras antiguas. Se conserva el límite compartido de 24 altas/día, incluidos fallos; varias confirmaciones simultáneas comparten cotizaciones iniciales.
- Informes con concentración, monedas/días únicos y sensibilidad hipotética a salidas desconocidas. `performance` usa SQLite de solo lectura, una instantánea coherente e intervalos explícitos; separa perfil, versión de entrada y plan.
- Nuevas cotizaciones guardan metadatos documentados y acotados. No se guardan transacciones ni blobs arbitrarios, ni se suman otra vez comisiones ya incluidas.

Se conservan los valores de `profiles.json` y `paper-policy.json`, límites, capital 600/300/100, cuotas y avisos de operaciones por Telegram. Los filtros de entrada siguen siendo hipótesis: esta revisión no los optimiza retrospectivamente sobre 70 cierres. El control de coste neto puede reducir entradas.

## Pruebas

Ejecutadas el 25/09/2026: 261 pruebas con Python 3.9 local (260 aprobadas y una omitida por la dependencia WebSocket) y las 261 aprobadas con Python 3.12 en un contenedor aislado del VPS, sin acceso a proveedores ni al volumen activo. `git diff --check` correcto. La reserva de cuota del estudio también se prueba con dos conexiones simultáneas: el límite se comprueba bajo bloqueo de escritura.

```bash
python3 -m unittest discover -s tests -v
git diff --check
docker run --rm --network none --entrypoint python memecoin-scanner:0.11.0 -m unittest discover -s tests -v
docker compose exec paper python server.py performance --since 2026-09-21T00:00:00Z --until 2026-09-26T00:00:00Z
```

Las regresiones cubren criterios exclusivos de entrada, riesgo crítico, pérdida de datos, cambio concurrente durante cotización, costes efectivos, salida pendiente tras reinicio, aislamiento por perfil, migración concurrente, confirmaciones sucesivas por perfil, cuotas, fallos de seguimiento, concentración extrema, lectura coherente con escrituras concurrentes y límites de fechas. La suite local puede omitir WebSocket si no está instalada su dependencia; la imagen incluye esa dependencia. Los resultados ejecutados se registran en la publicación correspondiente.

## Siguiente evaluación

Congelar los parámetros durante la recogida de datos v0.11. Examinar los cierres y los casos sin operaciones o sin datos, separando monedas, días, perfiles y versiones. Comparar costes, frecuencia de cierres por evidencia caducada, retorno neto y dependencia de la mejor moneda. Las distintas condiciones de mercado y universos entre versiones impiden atribuir causalmente toda diferencia al código.

No se fija una meta de operaciones para forzar entradas ni se promete que una muestra concreta bastará para demostrar rentabilidad. Las pruebas de software demuestran comportamiento reproducible; la mejora económica sigue pendiente de evidencia prospectiva.
