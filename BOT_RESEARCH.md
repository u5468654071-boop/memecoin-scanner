# Investigación de bots de memecoins — 27/09/2026

Se han revisado documentación oficial, repositorios de sus autores y estudios primarios. Existen herramientas de descubrimiento, ejecución, copy trading y bots abiertos. En las fuentes examinadas **no se ha encontrado un historial completo, reproducible y auditado que demuestre rentabilidad neta persistente de un bot equivalente a nuestro selector**. Esto describe el alcance de la revisión, no demuestra que ningún operador gane.

## Herramientas comerciales

| Herramienta | Función verificada y posible utilidad | Límite relevante |
| --- | --- | --- |
| [Axiom Pulse](https://docs.axiom.trade/axiom/finding-tokens/pulse) | Descubrimiento por etapa, liquidez, titulares y concentración; referencia para organizar señales. | Una terminal con filtros no proporciona umbrales de rentabilidad demostrada. No se verificó una API pública autorizada en la documentación revisada. |
| [Trojan](https://docs.trojan.com/terminal-overview/tools-and-widgets/copy-trading-bot) | Copy trading con límites por compra y filtros; ventas copiadas o reglas propias. | El seguidor llega después y puede obtener otro precio. Copiar a una cartera ganadora histórica puede introducir selección retrospectiva. |
| [BONKbot / Telemetry](https://docs.bonkbot.io/telemetry/trading/presets) | Configuraciones de compra/venta y TP, SL y trailing. | Una orden no garantiza precio ni ejecución. Sus [modos MEV](https://docs.bonkbot.io/bonkbot/settings/mev-protection) tienen compromisos de velocidad/protección. |
| [GMGN Agent API](https://docs.gmgn.ai/index/gmgn-agent-api) | Consultas de tokens, titulares, seguridad, pools, velas y carteras; posible fuente opcional. | La [OpenAPI actual](https://docs.gmgn.ai/index/cooperation-api-data-crawling-ip-whitelist) se anuncia abierta con 1 petición/s, cambios frecuentes y sin garantía empresarial. Consultar datos requiere API key; swaps requieren además firma. No se ha integrado en esta revisión. |
| [BullX NEO](https://bullx.gitbook.io/bullx-neo-docs/finding-tokens/explore-page) | Análisis de pares, volumen, concentración y riesgos. | No se ha verificado API pública. Sus términos restringen extracción no autorizada; no se automatiza su web. |
| [Jupiter](https://developers.jup.ag/docs/swap/order-and-execute) | API oficial de cotizaciones y rutas, ya utilizada por el simulador. | Optimizar una ruta no valida la selección del token. Cotización y ejecución confirmada son cosas diferentes; las tarifas aplicables se verifican en la respuesta. |
| [Fomo](https://fomo.family/) | Descubrimiento social y seguimiento de actividad. | No se identificó una API pública autorizada; sus [términos](https://fomo.family/terms) restringen automatización/extracción sin autorización. La fuente sigue siendo opcional. |

El límite de compras por moneda del [copy trading de GMGN](https://docs.gmgn.ai/index/copy-trade-copy-smart-money-automatically-earn-sol) se reinicia tras cerrar por completo. No equivale a una cuarentena después de pérdidas. Su blacklist puede impedir también ventas; nuestro experimento solo omite compras.

## Código abierto e infraestructura

- [Hummingbot Gateway](https://github.com/hummingbot/gateway) documenta conectores Solana/Jupiter/Raydium/Meteora. Su [v2.17.0](https://github.com/hummingbot/gateway/releases/tag/v2.17.0), del 22/09/2026, es una referencia útil para cantidades, slippage y estados. Es infraestructura, no una estrategia memecoin validada.
- [Chainstack pumpfun-bonkfun-bot](https://github.com/chainstacklabs/pumpfun-bonkfun-bot) contiene detección de tokens y ejemplos de ejecución. Sus autores lo describen como educativo y no destinado a producción. Comprar antes no garantiza comprar mejor.
- [warp-id/solana-trading-bot](https://github.com/warp-id/solana-trading-bot) aporta filtros de sniper como referencia histórica; el historial consultado termina en mayo de 2024. No se ha validado compatibilidad actual.
- [ChainBuff/open-sol-bot](https://github.com/ChainBuff/open-sol-bot) muestra seguimiento de carteras y colas; es educativo. No prueba rentabilidad del seguidor.
- [Jito searcher-examples](https://github.com/jito-labs/searcher-examples) aporta infraestructura de bundles; no un selector ganador. Sus [subastas y tips](https://docs.jito.wtf/lowlatencytxnsend/) son parte del coste y no garantizan inclusión.

No se ha instalado ni ejecutado código externo, contratado servicios ni conectado wallets. La fecha reciente, las estrellas y el código abierto no constituyen una auditoría de seguridad.

## Qué aporta la investigación científica

[Demystifying Solana Bots (ASE 2026)](https://arxiv.org/html/2607.28424v2) estudia repositorios y actividad on-chain, pero reconoce que sus repositorios y direcciones se recopilaron por separado y no pueden vincularse entre sí. Sus resultados de MEV no demuestran que un repositorio descargable obtenga ese beneficio.

[A Midsummer Meme’s Dream (USENIX Security 2026)](https://www.usenix.org/conference/usenixsecurity26/presentation/mongardini) estudia manipulación y muestra por qué una subida y un volumen elevados necesitan contexto. [Meme Coin Factories](https://arxiv.org/abs/2609.10246) analiza patrones coordinados en pump.fun. Son estudios de comportamiento y riesgos, no pruebas de que un filtro concreto vaya a ganar dinero.

[The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) explica el riesgo de elegir la configuración ganadora después de probar muchas. La aplicación práctica aquí es fijar pocas hipótesis antes de medir y conservar también sus resultados negativos.

## Decisiones para este proyecto

1. Implementar primero la [comparación prospectiva de cuarentena](SHADOW.md), con regla y período de inicio persistentes, cantidades/costes comunes y ganancias descartadas visibles.
2. Mantener separados señal, cotización y ejecución. [Solana](https://solana.com/docs/rpc/http/sendtransaction) advierte que enviar una transacción no garantiza que se confirme; el simulador no debe presentarse como una cuenta real.
3. Evaluar GMGN como posible fuente adicional de datos en una fase posterior, con permiso de consulta, presupuesto y validación de campos. No cambiar filtros porque otro proveedor muestre una etiqueta atractiva.
4. Medir concentración, repetición por mint y versiones. Diferenciar datos ausentes de cero y compradores de direcciones que pueden estar relacionadas.
5. No depender de scraping de terminales. Los informes completos de esta investigación no sustituyen una auditoría de seguridad ni una reproducción de los artículos.

La ingeniería de estas herramientas ofrece ideas verificables. La ventaja económica de nuestro selector sigue pendiente de evaluación prospectiva.


## Ampliación del 28/09/2026

- [Hour-Aware Adaptive Risk Management, versión 3](https://arxiv.org/abs/2606.08232v3): 190 operaciones ficticias, resultado positivo frágil al quitar los tres mejores cierres. La comparación exploratoria de horas no es significativa (p=0,5634 en v3); no copiamos vetos horarios. Su contribución útil es medir rechazados y fragilidad, no certificar un bot ganador.
- [solana-signal-trader](https://github.com/hypnogaba/solana-signal-trader): referencia de ingestión Telegram, checks y revisión por canales. No publica los canales de origen usados por el operador ni un historial auditado reproducido por nosotros. Su retuning no se traslada automáticamente a nuestro escáner.
- [solana-pumpfun-bot](https://github.com/DeeKalshiWay/solana-pumpfun-bot): declara resultados de paper trading con fricciones, pero también concentración de aproximadamente el 60% del PnL en un ticker. No hemos reproducido ese historial ni verificado ganancias reales. No se instala ni ejecuta su código.
- [MELT, versión 2](https://arxiv.org/abs/2602.13480v2): conjunto de trazas y features para detectar riesgo de lanzamientos y cuentas coordinadas. Una mejora de clasificación o reducción de pérdida no demuestra beneficio ejecutable. Nuestro muestreo de propietarios y grupos del proveedor sigue siendo incompleto; no fingimos disponer de su grafo/dataset en tiempo real.
- [Catching the Rug](https://arxiv.org/abs/2608.20271): analiza detección temprana y transferencia entre plataformas. Antes de incorporar ML harían falta features disponibles en tiempo de decisión, separación cronológica y coste/cobertura medidos en nuestro universo. No se han reproducido sus modelos.
- [Organic Score, explicación oficial de Jupiter](https://developers.jup.ag/blog/what-is-organic-score): distingue traders, compradores netos orgánicos y volumen. El score es relativo al ecosistema y puede ser volátil en tokens recientes. No tratamos un ratio de volumen orgánico inventado como filtro oficial ni lo confundimos con rentabilidad.
- [SwapHunt, estudio publicado por sus autores](https://swaphunt.dev/articles/solana-memecoin-null-result): informa resultados negativos al probar reglas sobre tokens descubiertos después de 250k de capitalización. Es evidencia autodeclarada con universo y supuestos de fricción propios, no una prueba de que toda estrategia falle. No hemos reproducido su base ni sus backtests.

### Aplicación concreta

Se añade diagnóstico de concentración por cierres, monedas y días, estrés de costes adicionales y asociación del PnL con evidencias de salida en [ROBUSTNESS.md](ROBUSTNESS.md). No se cambian filtros por selección retrospectiva de la configuración que gana en esta muestra. El experimento existente conserva su regla prefijada y sus resultados negativos; no se promociona a regla principal porque evite algunas pérdidas si descarta todavía más ganancias.
