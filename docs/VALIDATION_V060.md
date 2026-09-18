# Validación v0.6.0 — servidor y simulación

Fecha: 18 de septiembre de 2026. Base: `main` en `e59edec`. Los resultados de investigación previos se conservan en [VALIDATION_V051.md](VALIDATION_V051.md).

## Comprobado localmente

- 126 pruebas automáticas aprobadas en Python 3.9.6, con `websockets==15.0.1`; no necesitan claves ni servicios externos. Incluyen el servidor WebSocket local.
- Entradas ficticias con cotizaciones nuevas, identidad y cantidad exactas, señales caducadas/futuras, política y versión.
- Contabilidad en micro-USDC, slippage/comisiones hipotéticos, persistencia tras reinicios y rechazo de cambios de política sobre la misma cartera.
- Límites de posiciones, saldo, pérdidas realizadas/abiertas, señales duplicadas y pausa que llega durante una consulta.
- Salidas por stop, objetivo, trailing, tiempo, invalidación y cierre manual. Salto de precio cerrado a la cotización observada, no al precio del umbral.
- Ruta ausente: posición abierta, capital comprometido, valoración desconocida y posterior recuperación sin duplicar saldo.
- Rollback de posiciones y saldo ante fallo de escritura; copia consistente de SQLite y rechazo de sobrescrituras.
- Cuotas y separación entre consultas compartidas entre conexiones; pausas persistentes del proveedor y reserva de cuota para simulación.
- Bucle acotado de servicio, reinicio sin reinicializar capital, controles locales sin red y salud caducada/futura/detenida.

## Comprobado en GitHub

Las ocho comprobaciones del commit `2ad6ed3` han pasado (eventos push y pull_request): 126 tests en Python 3.9, 3.12 y 3.13, validación de Compose, construcción de la imagen, tests dentro del contenedor sin privilegios y dos arranques acotados con la misma cartera persistente. El código publicado coincidía exactamente con los archivos locales probados (árbol `ae962298c1767b3d25754304a8f05560ab9c90eb`).

Evidencia: [ejecución de CI](https://github.com/u5468654071-boop/memecoin-scanner/actions/runs/35359764670) y [job del contenedor](https://github.com/u5468654071-boop/memecoin-scanner/actions/runs/35359764670/job/105647850683). La comprobación Docker se hizo en GitHub; no hay Docker Engine en la máquina de desarrollo. La prueba del contenedor usa una clave ficticia y una cartera vacía, sin consultas externas.

## Pendiente en esta entrega

El despliegue y reinicio reales del VPS necesitan su acceso SSH y la configuración local de la clave Jupiter.

No se ha probado Jupiter autenticado en vivo ni obtenido una serie prospectiva de compras/ventas ficticias reales. Los tests usan cotizaciones sintéticas. No hay órdenes, firmas, wallet ni ejecución con dinero real. Los resultados no demuestran rentabilidad.
