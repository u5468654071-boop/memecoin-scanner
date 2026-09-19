# Validación v0.8.1 — búsqueda y medición prospectiva

19 de septiembre de 2026. Base: `main` en `8d1eda0`. Validación anterior: [VALIDATION_V070.md](docs/VALIDATION_V070.md).

## Pruebas reproducibles

204 pruebas pasan localmente con Python 3.9.6 y websockets 15.0.1. No necesitan claves ni APIs externas; el test WebSocket usa un servidor local.

```bash
python -m unittest discover -s tests -v
```

Se conservan las 167 comprobaciones anteriores y se añaden 37 para:

- Lotes con identidad exacta de tokens y pools, caché acotada y fechas de proveedor con nanosegundos compatibles con Python 3.9.
- Datos desconocidos que no aprueban, edad pendiente y riesgos conocidos independientes de campos ausentes.
- Creaciones fuera de la cola profunda, migraciones, cupos de confirmación y exploración, prioridad de posiciones abiertas y descansos que sobreviven a reinicios.
- Fallos de proveedores, ciclos sin candidatos, observaciones persistidas y repetidas después de reiniciar el escáner.
- Muestra fijada antes de consultar, cantidad exacta, costes, rutas ausentes, reinicios, cupos diarios, vencimientos y rechazo de respuestas tardías.
- Informes por versión, evaluación sin nuevos escaneos y conservación de saldos durante el estudio.

El test de stream antiguo podía terminar a los 250 ms antes de recibir mensajes en un runner lento. Ahora termina después de procesar el evento y su duplicado, con un límite de seguridad de 10 segundos. Mantiene la comprobación real de conexión y deduplicación.

La CI ejecuta Python 3.9, 3.12 y 3.13 y Docker con persistencia sobre el mismo volumen. El resultado del commit publicado se consulta en [Actions](https://github.com/u5468654071-boop/memecoin-scanner/actions); la existencia del workflow no demuestra por sí sola que haya pasado.

## Preflight con APIs reales

La prueba aislada del 18 de septiembre recogió 60 direcciones de Jupiter y preseleccionó 30 en un lote: 5 listas para análisis completo y 25 aplazadas por edad, pool, activo de cotización o liquidez. Las 3 analizadas después tenían actividad orgánica vigente y grupos de concentración reportados. Los tres perfiles las rechazaron por sus comprobaciones de riesgo. No se escribieron señales nuevas de entrada ni se modificaron saldos en ese preflight.

Es una comprobación pequeña del flujo, no una comparación estadística con v0.7. La versión añade tablas, conserva la huella del plan y los saldos 600/300/100, y comienza su propia confirmación temporal. Los archivos de configuración privados y los datos del servidor no se publican.

## Ajuste operativo v0.8.1

El primer arranque de v0.8.0 importó más de 900 migraciones pendientes y el orden global por antigüedad retrasaba las listas actuales. Se reserva ahora capacidad de preselección 20/10 entre listas y migraciones, cediendo los huecos sobrantes; las migraciones nuevas se atienden antes que las antiguas sin consultar. Dos pruebas adicionales reproducen el atasco y comprueban que no se desperdicia capacidad. No se modifican los filtros ni el protocolo prospectivo.

## Alcance

Las pruebas validan comportamiento de software con fixtures y fallos controlados. La disponibilidad real de datos se informa por separado. La actualización mantiene las políticas de riesgo y añade preselección, seguimiento y el protocolo de [RESEARCH.md](RESEARCH.md). No demuestra que los filtros sean óptimos, que la estrategia gane dinero ni que las cotizaciones se ejecuten a esos precios. Todo sigue en simulación.
