# Cuarentena por moneda: prueba prospectiva emparejada

Aplicación 0.11.1; decisiones del escáner 0.11.0, sin cambiar sus filtros ni salidas. Solo dinero ficticio.

## Hipótesis fijada antes de medir

Después de **dos cierres perdedores aceptados por el tratamiento**, de una misma dirección mint, en una ventana móvil de 24 horas, el tratamiento omite nuevas compras de esa moneda durante 24 horas, en los tres perfiles. Las ganancias no borran pérdidas de la ventana. Se cuentan cierres con PnL neto negativo, incluidos los supuestos de costes del simulador.

La ventana es `(t−86400,t]` y la cuarentena `[segundo cierre, segundo cierre+86400)`. Las pérdidas adicionales de posiciones que ya estaban abiertas no prolongan una cuarentena activa. Las salidas siguen registrándose; nunca se bloquea una venta. Dos cierres simultáneos de distintos perfiles cuentan como dos cierres, aunque no sean observaciones independientes.

## Qué compara y qué no

El control conserva todas las oportunidades que acepta el simulador principal a partir del inicio. El tratamiento hereda exactamente cantidad, coste y salida de esas oportunidades salvo que las descarte por cuarentena o por saldo insuficiente. Usa las cotizaciones que ya guarda el control: **cero consultas adicionales a proveedores** y ninguna orden real.

Es una comparación sobre oportunidades comunes, no dos bots autónomos. No busca compras alternativas con el capital liberado ni permite entrar cuando el control está detenido por sus propios límites. Por tanto, la diferencia mide esta regla de descarte sobre el flujo observado; no demuestra el rendimiento de una estrategia autónoma con otras decisiones. Si el tratamiento omite una operación ganadora, esa ganancia perdida se cuenta. Las omisiones por saldo se muestran separadas de la cuarentena.

## Inicio, aislamiento y reinicios

Compose habilita `--shadow-quarantine` únicamente en el servicio `paper` con perfiles. El experimento espera a que las tres carteras principales estén sin posiciones abiertas. En ese primer momento fija el saldo disponible de cada perfil, idéntico para ambos brazos, la versión, el plan, la regla y el último evento del diario. **No vuelve a cargar 1.000 USDC, no incorpora pérdidas anteriores al inicio y no reinicia la cartera principal.**

Las aperturas y los cierres nuevos se registran con una secuencia global, dentro de la transacción de su cuenta. El observador procesa esa secuencia en orden, de forma idempotente. Solo los cierres ya conocidos pueden afectar a una apertura posterior. Si queda retraso de procesamiento, se declara y no se presenta la comparación como actual.

Las tablas `shadow_*` son independientes de las cuentas `paper_*`. El observador usa su propia conexión SQLite y conserva cursor, saldos y cuarentenas entre reinicios. Cambiar el plan, la versión o la regla detiene la comparación en lugar de mezclar experimentos. Un fallo exclusivo del diario no bloquea el cierre del control si se puede guardar la marca de error; deja una marca persistente de datos incompletos que invalida el estudio. No se borra automáticamente.

No se solicita una venta para iniciar el experimento. Si no existe un momento sin posiciones abiertas, permanece a la espera. Quitar el flag deja de procesar la sombra; no elimina sus tablas ni modifica el control. No se crean tareas recurrentes de Codex ni se envían operaciones sombra como avisos de trading en Telegram.

## Consulta

```bash
docker compose exec paper python server.py shadow-report
```

El comando consulta una instantánea SQLite de solo lectura; no activa el experimento. Incluye fecha de inicio, regla fijada, último evento procesado, estado del observador, cuarentenas, saldos/PnL/cierres por brazo y perfil, y dos resultados complementarios: **pérdidas evitadas** y **ganancias descartadas**. Las oportunidades omitidas que todavía no han cerrado permanecen pendientes.

Las posiciones abiertas se valoran con su misma cantidad y la última marca del control. Una marca ausente, incompatible o caducada produce patrimonio desconocido, no cero ni el último valor presentado como actual. Los resultados no incluyen una ejecución real ni todos los costes y fricciones de red. La comparación se declara experimental, nunca una prueba automática de rentabilidad.

## Criterio de evaluación

Conservar esta hipótesis sin ajustar la ventana después de mirar cada resultado. Examinar el beneficio neto perdido/evitado, monedas y días distintos, dependencia de una sola moneda y omisiones por falta de saldo o de datos. Superar a un control perdedor no implica obtener beneficios, y las reentradas correlacionadas no equivalen a muestras independientes. Un resultado prometedor justificaría después una prueba de carteras autónomas, con su propio presupuesto de datos y modelado explícito de las nuevas oportunidades.
