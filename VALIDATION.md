# Validación de Memecoin Scanner v0.5.1

Fecha: 18 de septiembre de 2026. Base original: commit `9eda5fa` de `u5468654071-boop/memecoin-scanner`. Rama local: `improve/scanner-validation-and-tracking`.

## Resultado

Implementada una nueva ruta de ejecución predeterminada, manteniendo la anterior como `--legacy`. Añadidos adaptadores de solo lectura, motor de decisión, captura de eventos, persistencia de fases/alertas/cupos, cotizaciones y comparación prospectiva con dos referencias. Los históricos CSV originales no se modificaron.

**100 tests aprobados** en Python 3.9.6, incluyendo el WebSocket local con `websockets==15.0.1`. Compilación correcta, ayuda de ambos modos y comprobación de diff sin errores de espacios.

La revisión final corrige la confirmación temporal: una observación incompleta no puede alargar artificialmente el periodo confirmado; las caídas de liquidez se comparan con el máximo del tramo y las interrupciones no cuentan como actividad sostenida. Los informes separan también la política mejorada, la versión y los tamaños solicitados. Se rechazan tamaños USDC con más de seis decimales y ventanas imposibles antes de usar la red.

## Verificación con servicios reales

- Captura PumpPortal de 15 segundos: **seis eventos nuevos de creación guardados**. Solo se suscribieron creación y migración; no flujos de operaciones de pago. La prueba no recibió un evento de migración: esa transición se verificó con fixtures.
- Se tomó un token de esa cola y se ejecutó el análisis, sin introducir manualmente su dirección. El registro conserva la fase `bonding_curve` y los motivos de descarte/comprobaciones pendientes.
- Escaneo adicional de un token con pool establecido: respuesta real de DexScreener, resumen e informe completo de RugCheck, asociación al pool exacto y grupos relacionados reportados.
- La consulta RPC directa al mint respondió y permitió verificar programa y autoridades. La consulta posterior de holders recibió **HTTP 429** del RPC público: se conservó como incompleta y no se dio por verificada.
- No había `JUPITER_API_KEY` configurada. Por tanto **no se ha probado Jupiter autenticado en vivo** ni obtenido un candidato real que supere todas las comprobaciones v0.5. La CLI explicó esta falta de cobertura.

## Qué se verificó con datos sintéticos

Las pruebas automáticas no realizan operaciones ni llamadas a APIs externas.

- Identificación exacta de token, cadena, pool y ambos lados del mercado.
- Campos ausentes, autoridades activas, programas desconocidos, Token-2022 y extensiones no soportadas.
- Conversión de cuentas de token a sus propietarios, suma por dueño y exclusión de reservas identificadas.
- Grupos relacionados, ausencia de datos frente a lista vacía y concentración sin sumar grupos solapados.
- Jupiter: identidad de mint, frescura, score, cantidades enteras precisas, entrada/salida, rutas ausentes y consultas sin wallet.
- Tres observaciones distintas y espaciadas antes de una candidatura; cachés repetidas, datos futuros, cambio de pool y deterioro de liquidez.
- Flujo completo de observación a candidato con respuestas simuladas; expiración, deduplicación e invalidación de alertas.
- Persistencia entre conexiones, cuota diaria, 429/Retry-After, protección frente a dos escaneos simultáneos, bucle de duración acotada y rollback atómico de observación/alertas/evaluaciones.
- Evaluaciones a 1/6/24h, salida a nivel de mint, costes hipotéticos, plazos perdidos y comparación de referencias.
- Conexión real a un servidor WebSocket **local de prueba**: dos suscripciones gratuitas, mensajes inválidos, deduplicación y persistencia.

La CI está configurada para Python 3.9, 3.12 y 3.13. La ejecución local descrita aquí corresponde a Python 3.9.6; el estado de las ejecuciones remotas se puede consultar en la pestaña Actions del repositorio.

## Límites pendientes de datos o de alcance

- No hay resultados prospectivos suficientes para afirmar mejora de rentabilidad. Los pesos y umbrales siguen siendo heurísticos.
- Las relaciones entre wallets provienen de RugCheck. No se ha implementado un grafo propio de financiación ni una atribución de identidad.
- La LP del pool exacto es reportada por RugCheck; no se prueba de forma independiente el contrato/posición de bloqueo ni su duración.
- Los propietarios se resuelven sobre las mayores cuentas. No es un censo de todos los holders.
- La fase de bonding curve se observa, pero no genera candidaturas con un modelo propio de ese protocolo.
- No hay recuperación garantizada de huecos del stream; quedan documentados en el historial.
- Las salidas son cotizaciones indicativas con supuestos de costes, no fills ejecutados ni simulación completa del impacto propio.
- No se ha entrenado un modelo predictivo ni añadido envío a Telegram. Las alertas quedan en la base local y el JSON.

## Entrega

Proyecto completo, pruebas y documentación preparados para publicarse como versión de investigación. No se ha instalado un servicio permanente: la prueba de captura terminó y el modo continuo se inicia explícitamente con `--watch`. La ausencia de pruebas autenticadas de Jupiter y de resultados prospectivos impide considerarlo una estrategia de inversión validada.
