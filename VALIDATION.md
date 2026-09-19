# Validación v0.9.0 — cobertura y selección del universo

19 de septiembre de 2026. Base: `main` en `3ebbb29`. [Validación anterior](https://github.com/u5468654071-boop/memecoin-scanner/blob/3ebbb29fbaf8a2ea42635d1fe0d1fcbcbe35daa5/VALIDATION.md).

## Problema medido

Entre 08:10 y 10:06 UTC, v0.8.1 guardó 18 observaciones de 18 tokens, todas rechazadas. En 16 el Organic Score era cero y faltaban compradores orgánicos; en 13 faltaba un informe utilizable de grupos relacionados. Las cuotas de proveedores no estaban agotadas. La cola dedicaba capacidad a novedades sin actividad suficiente; su contador `ready` incluía evidencia caducada y tokens aún en descanso.

Es una ventana pequeña, no una explicación de todas las horas anteriores. El programa no suspende el escaneo los sábados. No se ha demostrado rentabilidad ni que sus umbrales sean óptimos.

## Cambios comprobados

- Nuevas listas `toptraded/5m` y `toptrending/1h`, con 100 resultados por consulta y hasta 30 direcciones por fuente después de excluir edades conocidas incompatibles. Identidades exactas y endpoints de solo lectura.
- Preselección conjunta por perfil: edad, liquidez, Organic Score y compradores orgánicos de 5 minutos. No combinar la edad tolerada del conservador con los requisitos de actividad del agresivo. Los datos ausentes aplazan y se pueden volver a consultar.
- Capacidad compartida entre fuentes de actividad, otras listas y migraciones; primeras consultas y revisitas se alternan. Los huecos sobrantes se ceden.
- Informe que distingue evidencia reciente, descansos, datos vencidos, rechazo del perfil y cotizaciones omitidas por controles previos.
- Un grupo de RugCheck cuyo importe supera el suministro sigue siendo incompleto, con el motivo `network_amount_exceeds_supply`. No se recorta a 100 % ni se sustituye por cero.

El plan de riesgo, capital 600/300/100, tamaños 50/25/10, controles LP, propietarios, redes, trayectoria y costes permanecen iguales. Las observaciones de versiones anteriores se conservan, pero no confirman candidaturas de la nueva versión.

## Pruebas reproducibles

212 pruebas pasan con Python 3.9.6 local y Python 3.12 dentro del contenedor del VPS. Se mantienen las 204 anteriores y se añaden ocho que reproducen los casos de esta revisión.

```bash
python -m unittest discover -s tests -v
docker compose exec paper python server.py coverage
```

La CI ejecuta Python 3.9, 3.12 y 3.13, más una comprobación Docker con persistencia. Consultar el estado del commit publicado en [Actions](https://github.com/u5468654071-boop/memecoin-scanner/actions).

## Prueba con proveedores reales

El preflight v0.9 encontró 27 direcciones de actividad y 23 de tendencia: 40 distintas. Seis pasaron la preselección; las tres examinadas a fondo fueron rechazadas por LP insuficiente, cambios extremos de precio y/o datos incompletos, según perfil. Las dos listas respondieron sin errores. No se guardaron señales de entrada ni se modificaron carteras; las llamadas se contabilizaron en la cuota compartida.

No es una comparación simultánea ni aleatoria con v0.8.1 y no permite afirmar mayor rentabilidad. Los datos reales cambian, por lo que no se espera reproducir los mismos recuentos. El estudio prospectivo solo cubre los tokens que alcanzan análisis profundo; no todos los descartados en preselección.

## Fuentes técnicas y límites

- [Jupiter Tokens V2](https://developers.jup.ag/docs/tokens/token-information): categorías, ventanas, lotes y fecha del primer pool. La fecha del primer pool no es necesariamente la fecha de creación del mint.
- [Organic Score](https://developers.jup.ag/docs/tokens): indicador del proveedor sobre actividad; no es probabilidad de beneficio ni prueba de identidades independientes.
- [RugCheck OpenAPI](https://api.rugcheck.xyz/swagger/doc.json): informes y redes relacionadas. Los grupos pueden solaparse y no sustituyen un grafo propio de balances verificados.
- [DexScreener API](https://docs.dexscreener.com/api/reference): pares y lotes. Se selecciona un pool del token base con activo de cotización admitido.

Las pruebas verifican comportamiento del software, no rentabilidad. Todo continúa en simulación y las cotizaciones independientes no garantizan ejecución real, costes completos ni ausencia de manipulación.
