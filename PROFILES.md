# Tres perfiles de simulación — v0.7.0

El VPS usa `profiles.json`: un escáner recoge datos compartidos y un simulador gestiona tres carteras independientes. El capital total inicial es **1.000 USDC ficticios**, repartido 60/30/10. No se conecta ninguna wallet ni se ejecutan transacciones.

## Capital y salidas

| Regla | Conservador | Equilibrado | Agresivo |
|---|---:|---:|---:|
| Capital inicial virtual | 600 | 300 | 100 |
| Importe por entrada, antes del coste fijo | 50 | 25 | 10 |
| Máximo de posiciones | 2 | 2 | 2 |
| Stop por pérdida observada | 12% | 18% | 25% |
| Objetivo por ganancia observada | 24% | 36% | 50% |
| Trailing: retroceso desde máximo | 8% | 12% | 18% |
| Activación del trailing | +15% | +20% | +25% |
| Permanencia máxima | 4 h | 2 h | 1 h |
| Bloqueo por pérdidas del día, USDC | 15 | 10 | 5 |
| Espera tras cerrar la misma moneda | 2 h | 1 h | 1 h |
| Slippage supuesto por lado | 0,5% | 0,5% | 1% |

Cada entrada y salida descuenta además 0,05 USDC ficticios. El stop se calcula sobre el valor neto observado, incluidos estos supuestos. Mayor capital asignado no significa invertirlo todo a la vez. Los nombres describen diferencias relativas: incluso el perfil conservador estudia activos de alto riesgo.

## Filtros de entrada

| Evidencia exigida | Conservador | Equilibrado | Agresivo |
|---|---:|---:|---:|
| Edad desde el primer pool observado | 1 h–7 días | 15 min–3 días | 2 min–24 h |
| Liquidez mínima, USD | 100.000 | 50.000 | 20.000 |
| LP bloqueada reportada del pool exacto | ≥95% | ≥90% | ≥80% |
| RugCheck score máximo | 15 | 25 | 30 |
| Transacciones / ventas mínimas en 1 h | 100 / 20 | 50 / 10 | 30 / 5 |
| Volumen mínimo en 1 h, USD | 10.000 | 5.000 | 1.000 |
| FDV / liquidez máximo | 20 | 40 | 60 |
| Volumen 1 h / liquidez máximo | 3 | 5 | 8 |
| Cambio de precio permitido en 1 h | −10% a +40% | −15% a +80% | −20% a +150% |
| Organic Score mínimo | 60 | 40 | 20 |
| Compradores orgánicos en ventana de 5 min | ≥20 | ≥10 | ≥5 |
| Dueño mayor de la muestra / diez mayores | ≤10% / ≤35% | ≤15% / ≤45% | ≤20% / ≤60% |
| Grupo relacionado reportado máximo | 15% | 20% | 30% |
| Coste máximo cotizado de ida y vuelta | 2% | 3% | 4% |
| Confirmación: duración / muestras mínimas | 10 min / 5 | 3 min / 3 | 1 min / 2 |
| Caída máxima de liquidez desde el pico observado | 8% | 12% | 20% |

Todos requieren autoridades mint/freeze revocadas, programa/extensiones interpretados, comprobación de propietarios, LP del pool exacto, informe de grupos relacionados y ausencia de alertas críticas. Los datos ausentes impiden aprobar: ser agresivo no desactiva estas comprobaciones. Las estadísticas de ventanas móviles no se suman como compradores nuevos, ni cuentan como confirmaciones varias respuestas de caché iguales.

La concentración medida es un límite inferior de la muestra, no una auditoría de todos los dueños. Organic Score es una clasificación relativa de actividad del proveedor; no una probabilidad de beneficio ni una garantía contra manipulación. Los filtros son **hipótesis iniciales**, pendientes de evaluación prospectiva. No se ha demostrado que sean los mejores ni rentables.

## Protección conjunta y funcionamiento

- Máximo de 180 USDC de coste comprometido entre las tres carteras y 70 en una misma moneda, incluidos costes de entrada. Si coinciden señales, el orden es conservador, equilibrado, agresivo. Una entrada previa también consume ese límite; por eso no todos pueden entrar en el mismo token.
- Se bloquean nuevas entradas al alcanzar 30 USDC de pérdidas del día UTC entre las tres carteras: pérdidas realizadas, sin compensar ganancias, más pérdidas abiertas. Los bloqueos individuales también se aplican.
- Cada ciclo revisa las salidas de los tres perfiles antes de abrir posiciones. Una salida pendiente o valoración caducada en cualquiera bloquea nuevas entradas en todos.
- Las señales de entrada caducan a los 60 s; las cotizaciones, a los 30 s; las valoraciones, a los 120 s. Si no hay precio utilizable, el patrimonio se muestra como desconocido, no como un beneficio o pérdida inventados.
- Se comparte el presupuesto y el ritmo de las APIs. Las cotizaciones de cada tamaño solo se piden si pasa las comprobaciones previas; que no se soliciten no equivale a aprobarlas.
- El descubrimiento añade el listado de actividad orgánica de Jupiter a lanzamientos, migraciones y fuentes existentes. Se prioriza el seguimiento de observaciones próximas a confirmar y se limita el trabajo histórico por tanda. El universo observado sigue siendo parcial y sesgado por sus fuentes.
- Telegram identifica el perfil en aperturas, cierres e incidencias, y envía una sola confirmación al activar el plan. La misma ID numérica en dos carteras no duplica ni oculta avisos.

Las salidas se intentan con las cotizaciones disponibles aproximadamente cada minuto. Los saltos de precio, rutas ausentes y cuotas pueden causar pérdidas superiores a los umbrales. No se modelan completamente MEV, congestión, gas ni impacto propio. Son operaciones ficticias.

## Informes y cambios del experimento

`docker compose exec paper python server.py report` muestra capital, exposición, resultado, porcentaje de aciertos, factor de beneficio y drawdown observado por perfil. Sin cierres no se inventa una tasa de aciertos. Sin pérdidas, el factor de beneficio es `null`, no infinito. El drawdown solo usa valoraciones disponibles; se cuenta cuándo faltan. Los resultados de cada perfil no son muestras independientes y los límites comunes influyen en las entradas.

El plan normalizado y su huella quedan persistidos: cambiar un filtro o presupuesto detiene los servicios hasta restaurar el plan. Para otro experimento se utiliza otro volumen, conservando el anterior. Las comparaciones de cotizaciones separan la huella del plan y los perfiles; no sustituyen la contabilidad de posiciones.

La migración automática desde la cartera única solo se permite si conserva los 1.000 iniciales y nunca tuvo posiciones. Se archiva su estado en las tablas anteriores y se crean 600+300+100, **sin sumar otros 1.000**. Si ya hay actividad, la migración se bloquea. Antes de actualizar, usa la copia SQLite descrita en [SERVER.md](SERVER.md). `pause`, `close-all`, `resume` y las copias abarcan ahora las tres carteras.

Uso directo, con variables del entorno configuradas:

```bash
python server.py scan --profiles profiles.json
python server.py paper --profiles profiles.json
python server.py report
```

Compose ya incluye esos argumentos. `paper-policy.json` conserva la compatibilidad con experimentos de una sola cartera, pero no se puede activar esa cartera sobre una base con el plan de tres perfiles inicializado.

Fuentes de los datos y conceptos: [Jupiter Tokens](https://developers.jup.ag/docs/tokens/token-information), [Organic Score](https://developers.jup.ag/blog/what-is-organic-score) y [tamaño de posición y stop](https://www.cmegroup.com/education/courses/trade-and-risk-management/proper-position-size). Estas fuentes no validan los umbrales elegidos ni la rentabilidad del sistema.
