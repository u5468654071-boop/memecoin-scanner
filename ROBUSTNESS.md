# Diagnóstico de resultados ficticios — v0.11.2

`python server.py performance` consulta SQLite en una instantánea de solo lectura, sin proveedores. Conserva la separación por perfil, versión de decisión y huella del plan. La versión de aplicación cambia; la de decisiones permanece en 0.11.0. No modifica filtros, carteras, cuarentena ni avisos Telegram.

Cada cohorte añade `robustness`:

- Media y mediana de PnL neto por cierre, en USDC, no porcentajes ni rendimientos de cartera.
- Hasta tres mejores operaciones **positivas**, su fracción del beneficio bruto y PnL sin ellas. Si hay menos, solo se eliminan las positivas existentes; nunca se eliminan pérdidas para mejorar artificialmente el resultado.
- PnL sin hasta tres monedas con mayor resultado neto positivo. Se agrupan todos sus cierres, incluidas pérdidas; aborda reentradas correlacionadas.
- PnL sin el mejor día positivo de **cierre UTC** y detalle diario. No equivale a un filtro por hora/día de entrada.
- Escenarios con 0, 0,01, 0,05 y 0,10 USDC adicionales por lado y operación cerrada. Se descuentan dos lados del PnL ya neto; no se vuelven a restar costes guardados ni slippage de cotizaciones. Son supuestos de sensibilidad, no estimaciones de tarifas reales. No reconstruyen decisiones de cuentas que hubiesen tenido otros saldos.
- Coste adicional por lado que consumiría el beneficio observado. Solo existe con PnL positivo y cierres; en pérdidas se informa motivo explícito en lugar de un presupuesto negativo de costes.

`exit_reason_pnl` desglosa todos los cierres por motivo registrado; esos grupos sí son disjuntos. `risk_check_pnl` agrupa los checks bloqueados/ausentes/en espera de la evidencia guardada. Cada posición se cuenta una vez por código y estado, aunque un check aparezca repetido. Varios checks de una salida reciben el mismo PnL: **sus grupos se solapan, no se suman**. Es asociación con la evidencia al cierre, no atribución causal ni demostración de que endurecer ese filtro en la entrada hubiese ayudado.

Quitar ganadores usa el futuro y no define una estrategia operable. Estas sensibilidades tampoco son pruebas de significación ni criterios automáticos para autorizar dinero real. No reconstruyen patrimonio histórico, costes reales o posiciones abiertas. Una cohorte sin cierres no tiene media/mediana ni presupuesto de costes conocidos. Las carteras antiguas y los datos ausentes conservan su tratamiento existente.

## Comprobación del 28/09/2026

Instantánea del VPS de las 17:57 UTC; ventana de siete días, decisiones 0.11.0, mismo plan, solo simulación:

| Perfil | Cierres | Monedas | PnL USDC | Mediana por cierre | PnL sin tres mejores cierres positivos |
| --- | ---: | ---: | ---: | ---: | ---: |
| Equilibrado | 23 | 8 | +17,125550 | −0,220208 | −11,857331 |
| Agresivo | 17 | 10 | −15,613282 | −0,905864 | −19,115595 |

En el equilibrado, los tres mejores cierres aportan el 61,67% del beneficio **bruto**. Quitar su mejor día positivo de cierres deja −4,498948 USDC. Son tres días de cierres y varias reentradas de las mismas monedas: todavía no hay evidencia sólida de ventaja persistente.

El experimento emparejado de cuarentena, desde su propio inicio, tiene 16 cierres control y 13 aceptados por tratamiento. Omitió tres oportunidades cerradas: pérdidas evitadas 1,580634 USDC y ganancias descartadas 9,364664; diferencia −7,784030. No se confunde esa ventana con la cohorte completa, ni se activa como regla principal. No se reajusta su ventana mirando este resultado.

Estos números fijan una comprobación histórica; el informe del servidor calculará los datos posteriores. La investigación y sus limitaciones están en [BOT_RESEARCH.md](BOT_RESEARCH.md).
