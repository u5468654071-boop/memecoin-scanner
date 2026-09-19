# Validación v0.7.0 — tres perfiles de simulación

18 de septiembre de 2026. Base de implementación: `main` en `cda7736`. La validación anterior se conserva en [VALIDATION_V060.md](docs/VALIDATION_V060.md).

## Pruebas reproducibles

167 pruebas pasan localmente con Python 3.9.6 y websockets 15.0.1. No necesitan claves ni servicios externos; la conexión WebSocket usa un servidor local. Ejecutar:

```bash
python -m unittest discover -s tests -v
```

Se conservan las 138 comprobaciones anteriores y se añaden 29 para:

- Aislamiento de decisiones, umbrales distintos, confirmación temporal por plan y tamaño, datos críticos ausentes y rechazo de riesgos en los tres perfiles.
- Una única recogida de evidencia por observación, cotizaciones por tamaño solo tras las comprobaciones previas y trabajo histórico acotado.
- Distribución 600+300+100, reinicios sin duplicar saldo, migración sin actividad y bloqueo si hubo movimientos o cambia el plan.
- Límites compartidos de exposición y pérdidas, todas las salidas antes de cualquier entrada y revisión del límite después de consultar la red.
- Salidas sin ruta, recuperación, pausa, invalidación por perfil, rollback de débito y posiciones, y controles que abarcan las tres carteras.
- Avisos con IDs independientes y activación única; informes por perfil y separación de cohortes por la huella del plan.

La CI de esta versión ejecuta Python 3.9, 3.12 y 3.13 y una prueba Docker con dos arranques sobre el mismo volumen, para comprobar persistencia del reparto. Consultar [Actions](https://github.com/u5468654071-boop/memecoin-scanner/actions) para el resultado del commit publicado; la existencia del workflow no implica que ya haya pasado.

## Alcance

Los tests de lógica usan mercados y cotizaciones sintéticos. El funcionamiento operativo se verifica por separado en el VPS: copia consistente, tres servicios, capital agregado, perfil de cada observación y entrega del aviso de activación. Los resultados en vivo se conservan en el volumen privado, no en este repositorio.

No se ha demostrado rentabilidad, superioridad de filtros ni ejecución real. Los perfiles comparten universo y límites; sus retornos no son ensayos independientes. Se necesita historial prospectivo suficiente y declarar cotizaciones ausentes, costes supuestos y períodos sin valoración. El sistema sigue siendo exclusivamente de simulación.
