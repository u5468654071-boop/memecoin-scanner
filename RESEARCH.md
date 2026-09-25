# Estudio prospectivo: protocolo inicial y revisión v0.11

El objetivo es comprobar si las selecciones y descartes contienen información útil sobre retornos posteriores. No existe todavía una ventaja demostrada ni un sistema ganador validado. Esta primera mejora corrige la recogida de datos y fija cómo se medirán; no optimiza filtros sobre los resultados de la misma muestra.

## Protocolo fijado antes de los resultados

- Identificador: `forward-10usdc-v1`. El código y el plan quedan asociados a cada muestra.
- Universo: monedas que llegan al análisis completo de Solana con un pool DEX, en fase `new_pool`, `recent_migration` o `established`. Excluye curvas iniciales y monedas que nunca pasan por ese análisis. La cola ya aplica filtros de disponibilidad y mercado: no representa todas las memecoins ni permite evaluar los descartes de esa preselección.
- Muestra: hash SHA-256 de estudio, versión, plan y mint, módulo 5 igual a cero; aproximadamente un 20% antes del límite. Máximo 24 altas por día UTC, una por moneda/plan/versión y tipo de estudio. El límite favorece las que llegan primero ese día; las versiones pueden tener muestras distintas.
- Se fijan las etiquetas de cada perfil (`selected`, estado y motivos) antes de conocer el resultado futuro. `selected` corresponde a `quality_pass` de esa observación. Un token que espera confirmación cuenta como no seleccionado en ese instante.
- Entrada hipotética común: 10 USDC, compra cotizada y ruta inversa para la cantidad obtenida. Observación inicial de hasta 60 segundos y cotizaciones de hasta 30 segundos. Se descuenta un 0,5% de cantidad al comprar y se añade un coste fijo de 0,05 USDC.
- Salidas: nuevas cotizaciones para esa cantidad exacta a 1, 2 y 4 horas desde la cotización inicial, con un plazo máximo adicional de 180 segundos. Se aplica otro 0,5% de deslizamiento supuesto y 0,05 USDC de coste fijo. Son escenarios alternativos, no tres ventas del mismo saldo.
- La muestra y sus horizontes se guardan antes de consultar la API. Una ruta ausente o un reinicio no borran el caso ni lo sustituyen por otra entrada favorable. Las consultas de salida fallidas se reintentan como máximo una vez por minuto dentro del plazo. Una respuesta posterior al plazo no se usa como precio histórico.

Los costes son supuestos reproducibles; las cotizaciones pueden incluir otros cargos del proveedor. No representan una ejecución, ni modelan completamente MEV, el impacto de nuestra compra, costes de cuentas o fallos al enviar transacciones. El estudio no tiene wallet, no modifica el dinero ficticio de las carteras y no envía avisos de operaciones.

## Primera observación y confirmación posterior

Hasta v0.10, `forward-10usdc-confirmed-v1` registraba únicamente la primera confirmación de cualquier perfil después de una muestra inicial no seleccionada. Una confirmación temprana del agresivo podía impedir estudiar después la del equilibrado o conservador. Sus registros y etiquetas se conservan como históricos.

Desde v0.11, cada perfil tiene un estudio `forward-10usdc-confirmed-v2:conservative`, `:balanced` o `:aggressive`. Cada uno registra su primera confirmación disponible bajo la cuota, con cotización y plazos propios. La muestra inicial `forward-10usdc-v1` permanece inmutable, aunque una confirmación coincida con ella. Los estudios reservados en la misma observación comparten la pareja de cotizaciones; una confirmación posterior solicita precios nuevos. Las salidas se calculan con los costes guardados en cada entrada, no con parámetros cambiados posteriormente.

Todos comparten el límite de 24 altas al día, incluidos fallos y reservas iniciales. Cada moneda puede tener como máximo una muestra de cada tipo por plan y versión. Si se agotó la cuota, la primera señal registrada podría ser posterior a la primera señal detectada. El límite se consume por registro de estudio, no por moneda única; confirmar varios perfiles puede reducir la diversidad diaria. Los informes los separan: pueden compartir monedas y no son muestras independientes ni una comparación causal entre señales y descartes.

## Interpretación del informe

```bash
docker compose exec paper python server.py coverage
```

`forward_study.groups` separa estudio, versión, huella del plan, horizonte y etiqueta de perfil. Publica muestras, casos vencidos, estados y cobertura de salidas. `quoted` dispone de cotización; `untrackable` no obtuvo una entrada utilizable; `unavailable` agotó el plazo tras fallos; `missed` perdió el plazo; `pending` sigue pendiente. Un fallo inesperado conserva la reserva inicial como no observable.

Las medias y medianas llevan el sufijo `observed_only`: omiten retornos desconocidos y pueden ser optimistas si desaparecen las monedas peores. Hay que publicar cobertura, fallos, tamaño de muestra y sensibilidad a costes conjuntamente. Las filas `all` y las de cada perfil se solapan: no se suman como operaciones independientes.

La revisión v0.11 añade monedas y días únicos, número de entradas cotizadas, salidas observadas y fallidas, mínimo/máximo y concentración de retornos positivos. `leave_best_mint_out` muestra qué ocurre al excluir retrospectivamente la mejor moneda; no selecciona operaciones ni prueba una estrategia. `missing_exit_loss_sensitivity` asigna hipotéticamente −100% a salidas definitivamente no observadas de entradas que sí tuvieron cotización. Excluye entradas sin cotizar y salidas pendientes, y nunca cambia el PnL registrado. La señal `insufficient_evidence` permanece verdadera: estas estadísticas descriptivas no certifican rentabilidad.

Las nuevas cotizaciones conservan un subconjunto acotado de los campos documentados de ruta, impacto, identificadores y comisiones para auditar anomalías. Los campos ausentes siguen ausentes; los importes ya incluidos en la cotización no se cobran de nuevo. Un retorno extremo no se elimina solo por ser extremo.

Las etiquetas no son una asignación aleatoria. Una diferencia entre grupos no demuestra que un filtro la causó. El estudio de 10 USDC tampoco mide stops, dimensionamiento, salidas parciales ni rendimiento de las carteras 600/300/100. Esos resultados se consultan por separado en `server.py report`. Las series anteriores de 1/6/24 horas no se mezclan con este protocolo.

## Antes de modificar una estrategia

Primero hay que acumular una muestra prospectiva con cobertura suficiente, incluyendo períodos sin oportunidades y fallos de datos. Después, cualquier hipótesis nueva debe fijarse con su versión, contrastarse en otro período y evaluarse con costes y pérdidas máximas. Cambiar repetidamente umbrales mirando los mismos resultados puede producir una aparente ventaja por casualidad. No se ha establecido todavía un tamaño de muestra que permita afirmar rentabilidad.

Quedan para experimentos posteriores entradas específicas para cada fase, salidas parciales y una comprobación independiente de grupos de wallets y bloqueo LP. Esta versión mantiene las reglas y asignaciones de los tres perfiles; mejora búsqueda, seguimiento y diagnóstico sin relajar los controles críticos.

Referencias técnicas de los lotes: [Jupiter Tokens](https://developers.jup.ag/docs/tokens/token-information) y [DexScreener API](https://docs.dexscreener.com/api/reference). Los límites y la disponibilidad de los proveedores se siguen comprobando en ejecución.

La interpretación de cotizaciones sigue [Jupiter Order & Execute](https://developers.jup.ag/docs/swap/order-and-execute). El riesgo de escoger retrospectivamente el mejor experimento está descrito en [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) y [The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551). Estos trabajos justifican separar experimentos y resultados posteriores; no validan este bot.

## Universo de selección v0.9.0

El estudio toma su muestra de los tokens que alcanzan análisis profundo. Desde v0.9 se añaden listas de actividad/tendencia y se exige compatibilidad conjunta con los umbrales básicos de un perfil antes del análisis. Los aplazados en preselección no reciben seguimiento prospectivo de precios: no se puede estimar el rendimiento de todo el universo descartado ni atribuir diferencias entre versiones únicamente a los filtros. La versión queda separada en cada observación. Se mantienen los tamaños, costes, horizontes y límites diarios del protocolo.

Mantener los parámetros del plan fijos durante la recogida de datos. Una muestra pequeña sin operaciones no permite optimizar umbrales ni afirmar ventaja. Cualquier cambio futuro de parámetros requiere una hipótesis explícita, una cohorte nueva y evaluación posterior con costes y rutas ausentes visibles.

## Ampliación opcional del universo v0.10.0

Fomo añade direcciones observadas en listas web, no evidencia de rentabilidad ni de compradores únicos. La recepción queda registrada con su hora de captura, fuentes y deduplicación. Se mantienen políticas y cuotas; la versión separa el nuevo universo de los históricos v0.9. Las listas renderizadas y una recogida horaria tienen sesgo de selección y pueden perder oportunidades entre capturas. Debe medirse el rendimiento prospectivo y el solapamiento con las otras fuentes antes de atribuirle una mejora. Véase [FOMO.md](FOMO.md).
