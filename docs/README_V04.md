# Memecoin Scanner v0.4

Escáner de **observación para Solana**: descubre tokens, descarta candidatos con riesgos o datos incompletos, explica su orden y registra qué ocurre después. Solo consulta datos públicos: no conecta wallets, no firma transacciones ni ejecuta operaciones. Python **3.9 o posterior**, sin paquetes externos ni claves de API.

## Empezar

Desde la carpeta del proyecto:

```bash
python3 memecoin_scanner.py --limit 20 --json-output data/ultimo_scan.json
```

Muestra los candidatos que pasan los filtros y los motivos de descarte. Es normal que ninguno pase. Los nuevos resultados se guardan en `data/`: SQLite para seguimiento, CSV para inspección y JSON opcional con todos los campos. Los CSV históricos de versiones anteriores se conservan sin modificarlos ni usarlos como validación de esta versión.

Otros ejemplos:

```bash
# Direcciones verificadas por ti, separadas por comas
python3 memecoin_scanner.py --tokens DIRECCION1,DIRECCION2

# Añadir una búsqueda, conservando las fuentes habituales
python3 memecoin_scanner.py --search 'consulta que quieras investigar'

# Solo búsqueda: no depender de la lista de promociones
python3 memecoin_scanner.py --candidates '' --search 'consulta'

# Cambiar la ventana de edad del PAR, no la del lanzamiento del token
python3 memecoin_scanner.py --max-age-hours 168 --min-liquidity 30000

python3 memecoin_scanner.py --help
```

`--limit` es por fuente; `--max-tokens` limita el total (60 por defecto). Las fuentes se intercalan para que una no ocupe todas las plazas. Se conserva si el token provino de boosted, profiles, una búsqueda o una dirección manual. La promoción nunca da puntos. Una búsqueda no es un feed completo de nuevos lanzamientos.

## Cómo decide

Primero debe haber datos completos y superar **todos** los filtros. Después se calcula una prioridad de investigación de 0–100, con su desglose visible en JSON/SQLite. Un 80 **no significa** un 80% de probabilidad de éxito.

| Condición | Valor inicial |
| --- | --- |
| Cadena con validación on-chain | Solana |
| Cotización del par | SOL envuelto, USDC o USDT, identificados por dirección |
| Edad del par | Entre 0,5 y 48 horas, sin redondear para filtrar |
| Liquidez reportada del par | Al menos 20.000 USD |
| RugCheck | Respuesta completa, sin `danger`, score normalizado ≤ 30 |
| LP bloqueada reportada por RugCheck | ≥ 80% |
| Transacciones en 1 hora | ≥ 30, con ≥ 5 ventas |
| Proporción de compras en 1 hora | Entre 20% y 90% |
| Volumen en 1 hora | ≥ 1.000 USD y ≤ 10 veces la liquidez |
| FDV / liquidez | ≤ 100 |
| Variación en 1 hora | Entre −30% y +100% |
| Puntos de riesgo de mercado | ≤ 4 |

Son **umbrales heurísticos iniciales, no parámetros optimizados ni validados por rentabilidad**. Se pueden ajustar con las opciones de `--help`; cada observación guarda la política usada. Bajar umbrales aumenta cobertura y puede admitir más riesgos. Otras cadenas se pueden inspeccionar, pero nunca aparecen como candidatas validadas hasta implementar su análisis on-chain.

La prioridad usa liquidez (30 puntos), número de transacciones (20), flujo en ambos sentidos (15), score de RugCheck (20) y LP reportada (15). Usa escalas acotadas y logarítmicas para liquidez y actividad. No premia subidas extremas, publicidad ni enlaces sociales. Cuenta transacciones, **no usuarios únicos**: los bots pueden fabricarlas.

El `combined_score` heredado mide señales de riesgo (alto = peor), solo existe si RugCheck está completo y no es el criterio de orden del ranking. `research_score` es la prioridad (alto = investigar antes) y solo se asigna a los que pasan el filtro. Ninguno predice rendimientos.

## Seguimiento: medir antes de confiar

Cada escaneo registra **todos** los candidatos intentados, incluidos los descartados y errores. Para cada observación se preparan consultas a 1, 6 y 24 horas del **mismo par y token**.

```bash
# Ejecutar periódicamente durante las ventanas pendientes
python3 memecoin_scanner.py --evaluate

# Consultar el resumen guardado sin llamadas de red
python3 memecoin_scanner.py --report
```

Esto no programa ninguna tarea por sí solo. Para recoger las ventanas, puedes ejecutar `--evaluate` cada 5 minutos desde tu planificador habitual, usando siempre la misma carpeta o el mismo `--db`. No solapes ejecuciones. Conviene evaluar antes de iniciar un escaneo largo. La tolerancia es el 20% del horizonte: por ejemplo, la consulta de 1h debe completarse entre los minutos 60 y 72. Se guarda el instante real de respuesta.

- `observed`: precio indicativo y liquidez positiva disponibles dentro de la ventana.
- `pending`: aún no vence o hubo un error temporal que se puede reintentar.
- `missed`: no se consultó a tiempo; no se inventa un precio histórico con el precio actual.
- `unavailable`: el par original desapareció o no tiene precio/liquidez utilizables en la consulta.
- `untrackable`: no había precio/par de referencia en el escaneo inicial.

El resumen separa seleccionados y descartados, por horizonte, versión y política. Muestra cobertura, estados, tokens distintos, mediana y peor variación bruta observada. No mezcla silenciosamente políticas distintas. Repetir un token varias veces no crea muestras independientes.

**No es un backtest de operaciones.** No incluye comisiones, slippage, MEV, tamaño de posición ni prueba de venta. Los resultados no disponibles permanecen en los recuentos, pero no tienen retorno calculable: la mediana de los observados puede tener sesgo de supervivencia. Un precio publicado no garantiza que puedas vender a ese precio, incluso con liquidez positiva. Todavía no hay evidencia prospectiva de que este ranking mejore la rentabilidad.

## Cambios respecto al original

- Los errores de RugCheck, LP ausente, NaN, infinitos y datos obligatorios desconocidos bloquean el filtro; no se interpretan como seguridad.
- El precio solo se toma de un par de la cadena correcta donde el token solicitado es `baseToken`. Se priorizan cotizaciones conocidas por dirección.
- Los enlaces de las webs/redes se cuentan como metadatos declarados. **No se visitan ni dan puntos**: evita solicitudes a direcciones arbitrarias controladas por terceros y la falsa etiqueta «comunidad verificada».
- Solicitudes limitadas a las dos APIs conocidas, sin redirecciones, con timeout, límite de respuesta, pausas y reintentos acotados. Se respeta `Retry-After`; si supera 30 segundos se devuelve fallo temporal en vez de reintentar demasiado pronto. Una fuente caída no impide usar otra.
- Historial principal en SQLite con transacciones; CSV con cabecera estable, archivos de migración únicos y escape de fórmulas en texto externo. No se sobrescriben históricos anteriores.
- Tests reproducibles sin red y workflow de CI para Python 3.9, 3.12 y 3.13.

## Límites de las fuentes

DexScreener aporta precios y actividad reportados, no identidad de compradores ni garantía de volumen auténtico. Boosted y profiles tienen sesgo de autopromoción; las búsquedas también tienen cobertura limitada. La edad es del pool, no del token; una migración puede crear un par reciente para un token antiguo.

El resumen de RugCheck es una comprobación externa. No reejecutamos una auditoría on-chain, no verificamos su frescura y `lpLockedPct` **no demuestra por sí solo que el pool elegido esté bloqueado ni durante cuánto tiempo**. No prueba ausencia de insiders, bundles, contratos maliciosos ni posibilidad de venta. Los precios de seguimiento pueden proceder de cachés del proveedor; se registra la hora de recepción, no una hora on-chain independiente. Los filtros no son una garantía contra estafas.

Referencias de integración: [API de DexScreener](https://docs.dexscreener.com/api/reference), [API de RugCheck](https://api.rugcheck.xyz/swagger/index.html) y [esquema JSON de RugCheck](https://api.rugcheck.xyz/swagger/doc.json).

## Pruebas y códigos de salida

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile memecoin_scanner.py tracking.py
```

Las pruebas usan datos sintéticos identificados y no llaman a las APIs. Cubren fallos de red, reintentos, filtrado conservador, selección del par, persistencia y seguimiento temporal.

Salida `0`: ejecución completada, aunque haya cero seleccionados o fuentes parcialmente caídas. Salida `2`: argumentos inválidos, ningún dato de mercado utilizable o ningún informe completo de RugCheck en un escaneo de Solana. Los fallos de escritura se propagan; no se ocultan como éxito. Consulta los errores por token y por fuente en JSON; un `0` no significa cobertura completa.

`data/` está excluido de Git. Usa una sola instancia por conjunto de archivos CSV: SQLite protege transacciones, pero el CSV no implementa bloqueo entre procesos. Nunca se necesitan claves privadas o seed phrases.
