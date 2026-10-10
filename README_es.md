🇬🇧 [English](README.md) · 🇪🇸 [Español](README_es.md)

# Análisis de la cobertura arbórea en Monte Hermoso 🌳🛰️

Un pipeline abierto y reproducible que detecta el cambio anual de la vegetación
leñosa (árboles y arbustos) en el **casco urbano de Monte Hermoso, sus
alrededores y Balneario Sauce Grande** (unos 46,5 km² de los 208 km² del partido,
en la costa sur de la provincia de Buenos Aires, Argentina), de 1985 a 2026,
usando **solo datos libres y públicos** procesados en Google Earth Engine. Busca
el espacio verde perdido por desarrollo inmobiliario e intenta que las
oscilaciones de las sequías no se lean como pérdida. El resto del partido queda
como contexto.

> **Estado: prototipo de investigación.** El pipeline está resuelto, es rápido
> y reproducible, pero la precisión de la detección **todavía no está
> validada**: la revisión manual que la mide es hoy el cuello de botella (ver
> [Estado actual](#estado-actual-y-validación-)).

Cada evento detectado es un **cambio candidato** en la señal satelital. El
proyecto no da por hecho que sea una pérdida de árboles: revisar una muestra
etiquetada es lo que dice cuáles lo son.

![Monte Hermoso y Sauce Grande, 1986-2026](docs/monte_hermoso.gif)

Un cuadro por verano (Sentinel-2 a 10 m desde 2017, Landsat a 30 m antes, con
los huecos de nubes rellenados con años vecinos, por eso los primeros cuadros
pueden mezclar épocas), en dos paneles a la misma escala, con la pérdida
persistente de árboles en rojo y la ganancia en verde, acumuladas desde 1986, y
debajo la cobertura arbórea de la zona de estudio. Hay un mapa interactivo con
control de años en `docs/index.html`: se sirve con
`python3 -m http.server -d docs`, o activando GitHub Pages en `main` `/docs`;
también superpone los eventos Sentinel-2 de 10 m confirmados, con la vegetación
que pasó a construido en naranja.

## Hallazgos principales 🔎

Todas las cifras son de la zona de estudio. Son señales satelitales contrastadas
con una revisión manual chica, no un inventario de árboles.

- **La cobertura arbórea creció durante décadas y cae desde 2023.** De 506 ha en
  1986 a 1.182 ha en 2023 (más del doble), y 1.045 ha en 2026 (-137 ha, -12%).
  El resto del partido cayó 21% en esos mismos años, así que la baja reciente no
  es solo urbana. Los últimos tres años son provisorios.
- **La subida es sobre todo forestación, no arbolado de ciudad.** MapBiomas
  muestra pastizales que pasan a plantación y bosque abierto (148 ha entre 2010 y
  2023, casi sin pérdida), lo que encaja con los trabajos de la UNS sobre
  vegetación introducida que avanza sobre los médanos. Una parte es recuperación
  de la sequía de 2009 y otra probablemente es densificación del follaje por
  encima de nuestro umbral de NDVI; no podemos separarlas. A 30 m no se ven los
  árboles de calle ni el replante 3 a 1.
- **El desarrollo inmobiliario explica poco de la pérdida.** 136 de 749 eventos
  (16,9 de unas 265 ha) se atribuyen a desarrollo, y se confirmaron a ojo dos
  desarrollos chicos de 2020. Un lote suelto queda bajo el mínimo de 500 m², y los
  grandes sectores de Monte Hermoso del Este se hicieron antes de la serie de 10 m.
- **Otras causas confirmadas:** desmonte en Los Bosques (unas 5 ha en 2020) y la
  apertura de un camino. La mancha de 31 ha de diciembre de 2025 y el pico de 2024
  no tienen causa conocida y esperan imágenes de mayor resolución.
- **Las sequías y la estacionalidad son la principal fuente de falsos positivos.**
  Cerca de la mitad de las hectáreas de 2020, al este de Balneario Sauce Grande,
  eran clima. Ninguna regla simple los separó de la pérdida real sin descartar
  cerca de un tercio de los reales, así que el detector no cambió salvo una caída
  mínima de NDVI para `cva`.
- **Ordenanza 1913 (2010): inconcluso.** Los intervalos de antes y después
  incluyen el cero, no hay municipio de control y a 30 m no se ve el replante.
- **La precisión está medida solo en parte.** De 37 eventos revisados,
  `veg_to_built` acertó 10 de 10 y `ndvi_diff` 5 de 8, `cva` 5 de 9 y `dynamic_world` 5 de 10 (intervalos anchos); 2024
  casi no está revisado.

## Zona de estudio 🗺️

La zona es la huella urbana actual más un margen de 1 km, así que incluye
manzanas nuevas, el monte de pinos y médanos pegado al pueblo y el tramo de
costa entre las dos localidades (46,5 km², `data/aoi/zona_urbana.geojson`,
también como KML para Google Earth). Se arma con
`python3 -m gee_mh.aoi --zona`:

- **Huella:** clase urbana de MapBiomas Argentina Colección 3 (2025) o
  probabilidad `built` de Dynamic World mayor que 0,35 (2024-2026), cerrada 150 m
  para unir manzanas vecinas; solo cuentan las manchas de más de 30 ha.
- **Recortada** al límite del partido (IGN, vía Georef), extendido hacia el sur
  porque su borde sur recto corta tierra en Balneario Sauce Grande, y sin el mar
  (agua de Dynamic World).
- **Los Bosques** (El Americano, 554 ha) se suma con su parcela 1050c del plano de
  zonificación municipal (484 ha), trazada del PDF y georreferenciada contra la
  laguna, más un buffer de 250 m por el error de la georreferencia.
- **Sesgo de selección:** la zona se eligió porque hoy tiene construcción, así
  que sus tasas no son comparables con las del campo. Las comparaciones de abajo
  son contexto, no un control.

## Por qué importa 🌊

La playa de Monte Hermoso está respaldada por el campo de dunas de los *Médanos
Blancos* ([sitio municipal](https://montehermoso.gov.ar/sitio/atractivos/medanos-blancos/)),
y entre Monte Hermoso y Coronel Rosales se extiende la Reserva Natural Pehuen
Có–Monte Hermoso, un área protegida provincial de 2.000 ha
([Wikipedia](https://es.wikipedia.org/wiki/Reserva_natural_Pehuen_C%C3%B3-Monte_Hermoso)).

A lo largo de la costa bonaerense los médanos se forestaron en gran escala con
árboles no nativos: álamos, eucaliptos, acacias australianas, tamariscos y,
sobre todo, pinos
([Wikipedia](https://es.wikipedia.org/wiki/Dunas_costeras_bonaerenses)). Por eso
"cobertura leñosa" acá mezcla exóticas plantadas, arbustos y arbolado urbano, y
una disminución no es automáticamente un daño ecológico: puede ser un desmonte
urbano, la remoción de una plantación o hasta una restauración del médano. Este
proyecto mide **dónde cambia la cobertura leñosa** y deja la interpretación a
quienes gestionan el territorio.

A nivel local, la Ordenanza 1.913 de Monte Hermoso (2010) regula las acciones
que alteran la forestación y la topografía natural, veda alterar el cordón de
dunas y exige reponer tres árboles por cada uno retirado, según la prensa
([La Nueva, 26-08-2010](https://www.lanueva.com/nota/2010-8-26-9-0-0-limitaciones-al-retiro-de-arboles));
la ordenanza sigue vigente. Un registro anual abierto y auditable de dónde cambia la cobertura leñosa puede
ayudar a contrastar permisos y reposiciones.

## Objetivos 🎯

1. Producir una serie anual (de 1985 a hoy) de eventos de cambio de cobertura
   leñosa para la zona de estudio urbana, como GeoJSON y CSV, con área en m² y
   hectáreas, y usarla para comparar los períodos anterior y posterior a la
   Ordenanza 1913 (2010).
2. Usar solo datos libres y gastar la mínima cuota de Earth Engine, para que
   cualquier municipio, escuela u ONG pueda repetirlo.
3. Medir con honestidad cuán precisa es: revisión visual estratificada de los
   eventos detectados, más contrastes con productos públicos independientes.
4. Informar los árboles como un **rango/escenario derivado del área de cambio
   detectada**, nunca como un conteo observado (los píxeles de 10 m no resuelven
   copas individuales).
5. Atribuir causas (desarrollo urbano vs. natural) solo cuando la evidencia lo
   respalda; si no, informar `desconocido`.

## Enfoque: solo datos libres y abiertos 🛰️

| Uso | Dataset (id en Earth Engine) | Licencia | Crédito |
|---|---|---|---|
| Imágenes desde 2015 (10 m) | Sentinel-2 SR Harmonized (`COPERNICUS/S2_SR_HARMONIZED`) | Términos de datos Copernicus Sentinel | Contains modified Copernicus Sentinel data |
| Imágenes desde 1985 (30 m) y la serie larga | Landsat 5/7/8/9 Collection 2 Level-2 | Dominio público (USGS) | Courtesy U.S. Geological Survey |
| Ajuste de la serie larga | LandTrendr (`ee.Algorithms.TemporalSegmentation.LandTrendr`) | Apache-2.0 (parámetros de LT-GEE) | Kennedy et al. 2010 |
| Lluvia (contexto de sequías) | CHIRPS Daily (`UCSB-CHG/CHIRPS/DAILY`) | Dominio público | Funk et al. 2015 |
| Máscara de nubes | Cloud Score+ (`GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED`) | CC BY 4.0 | Pasquarella et al. 2023 |
| Cobertura previa y baseline (desde 2015) | Dynamic World v1 (`GOOGLE/DYNAMICWORLD/V1`) | CC BY 4.0 | Brown et al. 2022 |
| Contraste independiente (1985-2025) | MapBiomas Argentina Colección 3 | CC BY 4.0 | Project MapBiomas Argentina |
| Contraste anterior (hasta 2023) | MapBiomas Pampa Colección 4 | Términos de MapBiomas (CC BY-SA; sus páginas son inconsistentes) | Project MapBiomas Trinational Pampa |
| Contraste independiente | Hansen Global Forest Change (`UMD/hansen/global_forest_change_2025_v1_13`) | CC BY 4.0 | Hansen et al. 2013 |
| Límite del partido | IGN Argentina vía Georef (datos.gob.ar) | CC BY 4.0 | Servicio Georef – argentina.gob.ar/georef (modificado: campos agregados) |

Las citas completas están en [NOTICE](NOTICE).

## Pipeline 🏗️

```
Earth Engine: Sentinel-2 / Landsat / Dynamic World / Cloud Score+
   |  1 pedido por año: compuesto de verano sin nubes (mediana, +-30 días del 15 de feb)
   v
caché en disco (data/cache)  -->  SatelliteImage: reflectancia, NDVI/NDBI/NDMI,
   |                              etiqueta y probabilidad de árbol de Dynamic World
   v
detección (4 baselines: ndvi_diff | dynamic_world | cva | veg_to_built), cada uno refinado con
   - corrimiento regional de NDVI del par descontado (sequías)
   - máscara leñosa estable (leñosa en los años t-1 y t)
   - chequeo de persistencia (la caída debe seguir en el año t+1)
   - recorte al polígono del partido, área mínima del evento
   v
eventos (polígonos exactos, causa por polígono)  -->  muestra estratificada + review_url  -->  precisión por estrato
   +-- contraste con MapBiomas Argentina C3

Compuestos de verano Landsat 5/7/8/9 1985-2026 --> ajuste LandTrendr por píxel
   --> pérdida/ganancia persistente de árboles, lluvia (CHIRPS), área urbana MapBiomas
   --> tasas anuales, antes/después de la Ordenanza 1913, animación y mapa web
```

## Notas de ingeniería 🧭

- **Una descarga por año**, no por tile: grilla exacta con `computePixels`
  (bandas crudas como `uint16`, una etiqueta de cobertura `uint8`, una
  probabilidad de árbol `uint8` y una de construido `uint8`). Una corrida
  completa antes tardaba unos 30 minutos y fallaba por un desajuste de tamaño
  entre tiles; ahora las imágenes de 2015-2024 bajan en unos 2,5 minutos en
  total (medido), y una nueva corrida sale de la caché en disco sin pedir
  imágenes a Earth Engine.
- LandTrendr sobre todo el partido en un solo pedido no terminó en minutos; el
  mismo trabajo en tramos de 100.000 píxeles tarda cerca de un minuto por tramo
  y después queda en caché.
- Reintentos con espera creciente, timeouts y un checkpoint tras cada año
  procesado: un fallo no pierde el trabajo ya terminado.
- Los años corren en paralelo (4 hilos, conservador para la cuota no comercial).
- Los pares de años con distinta resolución de sensor (30 m vs. 10 m) se
  omiten, porque el NDVI no es comparable entre ellos.

## Estado actual y validación 📊

Dos pistas responden una misma pregunta: ¿dónde cambió la cobertura verde de la
zona, y fue por desarrollo?

| Pista | Años | Resolución | Uso |
|---|---|---|---|
| Detalle | 2017-2026 (2016 es una sola escena) | Sentinel-2 10 m + Dynamic World | Eventos candidatos, atribución de causa, revisión manual |
| Larga | 1985-2026 | Landsat 30 m, ajustado con LandTrendr y corregido por sensor | Tasas anuales, sequías, antes/después de la ordenanza de 2010 |

### Pista de detalle, 2016-2026 (10 m)

Eventos dentro de la zona según el año al que llegan (`year_to`), por baseline,
con un mínimo de 5 píxeles (unos 500 m²; un lote suele medir de 600 a 1.800 m²).
Un evento está confirmado cuando el cambio sigue presente el verano siguiente,
así que 2026 no se puede confirmar hasta que exista 2027. `veg_to_built` es
cualquier vegetación, no solo leñosa, que pasa a construido.

| Año | `ndvi_diff` | `dynamic_world` | `cva` | `veg_to_built` | Total |
|---|---|---|---|---|---|
| 2017 | 41 | 11 | 42 | 6 | 100 |
| 2018 | 5 | 1 | 2 | 1 | 9 |
| 2019 | 14 | 0 | 8 | 5 | 27 |
| 2020 | 55 | 11 | 33 | 4 | **103** |
| 2021 | 4 | 0 | 4 | 0 | 8 |
| 2022 | 16 | 0 | 15 | 7 | 38 |
| 2023 | 23 | 1 | 17 | 7 | 48 |
| 2024 | 130 | 5 | 129 | 2 | **266** |
| 2025 | 2 | 0 | 1 | 0 | 3 |
| 2026 | 86 | 2 | 51 | 8 | 147 (sin confirmar) |

El compuesto de 2016 es una sola escena Sentinel-2 (marzo de 2016), así que
2016→2017 es solo indicativo. En total son 749 eventos, 602 confirmados; el
evento mediano mide unos 1.000 m².

Cómo separa el detector las sequías de la pérdida:

- **Corrimiento regional de NDVI medido en todo el partido, no en la zona.** Un
  verano seco o atípico baja el NDVI de toda la vegetación a la vez. Cada par
  resta la caída mediana de NDVI de los píxeles leñosos estables de todo el
  partido (0,116 en 2016→2017, 0,040 en 2019→2020, 0,066 en 2023→2024, 0,031 en
  2025→2026; una suba de NDVI no se resta). Medirlo dentro de la zona restaría un
  cambio real de toda la zona a sí mismo como si fuera clima.
- **Persistencia.** La caída debe seguir presente el verano siguiente.
- **Caída mínima para `cva`.** Un evento `cva` debe perder al menos 0,20 de NDVI en
  promedio (`CVA_MIN_NDVI_DROP`). En 31 eventos `cva` revisados a ojo eliminó los 22
  falsos positivos estacionales y perdió 3 de 9 reales, los tres menores de
  1.100 m². El umbral se eligió con esas mismas etiquetas, así que todavía no hay
  una verificación aparte. `ndvi_diff` no tiene este filtro y conserva la mayoría de
  los falsos positivos que quedan (sus 33 eventos al este de Balneario Sauce
  Grande en 2020).
- **Polígonos exactos y causa por polígono.** Cada evento se atribuye por
  separado, con la fracción de su polígono que Dynamic World llama construido en
  los dos veranos siguientes.

Lo que sabemos:

- **Dos picos, 2020 y 2024.** El par 2024 también es el más alto en todo el
  partido: el NDVI mediano de los árboles estables en 2018-2019 fue 0,545
  (2023), 0,482 (2024), 0,488 (2025), 0,470 (2026) y no se recuperó, aunque la
  lluvia (CHIRPS, septiembre a marzo) fue 378 mm en 2023, 399 mm en 2024, 524 mm
  en 2025 y 499 mm en 2026. Revisión visual en Google Earth Pro de 2020: el sitio más grande (2,5 ha en Los
  Bosques) es pérdida real de vegetación; los 33 eventos `ndvi_diff` al este de
  Balneario Sauce Grande (7,5 de las 14,9 ha de ese baseline), incluida la mancha
  `cva` de 4,5 ha, son efectos estacionales o de clima, así que cerca de la mitad
  de las hectáreas de 2020 son falsos positivos. El centro de Monte Hermoso (15
  eventos) no se revisó. 2024 sigue sin explicación y necesita
  la misma revisión.
- **Manchas grandes en 2026.** El evento más grande de 2026 mide unas 31 ha al
  noroeste de Balneario Sauce Grande (-38,983, -61,230): matorral verde en la
  imagen de 2025 que pasa a marrón en 2026. El NDVI del polígono cayó de 0,47 a
  0,13 en cinco días de diciembre de 2025 y la cicatriz sigue visible en abril de
  2026, aunque el pasto rebrotó. La causa es desconocida (no hay nota de prensa;
  un foco de calor de NASA FIRMS el 18 de diciembre está a menos de 3 km, lo que
  no la resuelve). Otro de unas 6 ha está cerca del Camping Americano. Ambos están
  sin confirmar.
- **El desarrollo inmobiliario es chico y difícil de ver.** 136 eventos (16,9 ha)
  terminan con al menos el 30% de su polígono construido y se etiquetan
  `desarrollo_inmobiliario`; 1 pasa a agua o costa; los otros 612 (248 ha)
  quedan `desconocido`. `veg_to_built` encuentra 40 eventos, 5,0 ha en total
  (casi todos los años entre 0 y 7 eventos). Una casa en un lote queda muy por
  debajo del mínimo de 500 m²; un macizo despejado para un loteo sí se vería.
- **Desarrollos conocidos** (`data/reference/desarrollos_inmobiliarios.csv`,
  ubicados con la prensa, OpenStreetMap y tu confirmación;
  `python3 -m gee_mh.developments` cuenta los eventos a menos de 300 m):
  - **Habitar Monte Hermoso** (Camino Sinuoso Oeste, junto al centro astronómico;
    anunciado en 2022, obras de 2022 a 2025): 14 eventos, 25 ha, en 2023 (6), 2024 (3) y 2026 (5); 9 caen a menos de dos años del inicio y 6 se
    etiquetan `desarrollo_inmobiliario`. Es la única coincidencia clara. El macizo
    anunciado es de unas 10 ha, así que el radio de 300 m también recoge cambios
    vecinos.
  - **Monte Hermoso del Este** (402 ha; iniciado a principios de los 90 según los
    papers de la UNS, planos municipales desde 1992 según el desarrollador; límites
    del mapa público del desarrollador, que también da sus sectores): 92 eventos adentro o a menos de 300 m. Fechas por sector: El Viejo Vivero (42 ha, primera
    etapa, principios de los 90, inferido): 20 eventos; Aldea del Este (8 ha,
    2008-2009): 18; Frente Marino (26 ha, primer edificio en 2008, torres promocionadas en 2022): 54; Pinar del Golf (31 ha): 47 eventos, 6
    `veg_to_built`, sin año de inicio publicado. Todos empiezan antes de la serie de
    10 m, así que los eventos son relleno dentro de sectores ya vendidos (lotes
    construidos sobre pinar), no el desmonte original; no hay nada que cruzar por
    año. Como los sectores son vecinos, un evento puede contarse en varios.
  - **Las Dunas** (unas 90 ha entre Del Dientudo y Las Ballenas, según las calles
    límite del paper de la UNS; su superficie construida se duplicó entre 2004 y
    2011): 19 eventos, 2 `veg_to_built`, ninguno en ese período porque la serie
    empieza en 2016.
  - **Bungalow** (70 viviendas Pro.Cre.Ar II sobre el antiguo barrio Bungalow, obra
    desde marzo de 2022) y **Las Lomas** (privado; presentado en febrero de 2006
    donde estuvo el camping Montesol, 408 lotes en 51 ha, rezonificado por la
    ordenanza 1559/2006, 55 lotes vendidos en 2008): 0 eventos. Ambos se
    construyeron sobre suelo ya ocupado o anterior a 2016, así que un cero es lo
    esperable, no una omisión.
  - **El Americano / Los Bosques:** el loteo original (hasta 933 lotes en una
    parcela de 507 ha junto a la reserva Pehuen Co y la laguna) se habilitó por la
    ordenanza 2418/2016, en enero de 2016. La parcela 1050c, trazada del PDF del
    plano de zonificación municipal y georreferenciada contra el contorno de la
    laguna (error de unos 250 m, área de 484 ha frente a las 507 publicadas), queda
    a 335 m del camping y llega a la laguna, lo que encaja con donde se describió
    Los Bosques; ninguna fuente la nombra. La zona ahora la cubre entera. El detector encuentra ahí 78 eventos (34,5 ha),
    ninguno `veg_to_built` ni etiquetado como desarrollo: 2017 (28, solo indicativo), 2020 (7, 10,9 ha), 2024 (13, 7,3 ha), 2026 (26, 10,2 ha). Un concejal dijo en abril de 2026 que la
    Provincia rechazó y recortó ese loteo y que el proyecto actual abarca unas 49
    ha.
  - **Villa sustentable de Sauce Grande** (zona DUE4 del código urbano de 2023,
    planificada en 2022, sin confirmar que se construyó): 11 eventos a menos de 300 m, 3 a menos de dos años de 2022. El punto es aproximado; esto no dice
    nada de la villa en sí.
  - Los polígonos están en `data/reference/desarrollos_poligonos.geojson`. Las
    Dunas y Bungalow son rectángulos aproximados desde extremos de calles, no
    límites catastrales.
- La coincidencia con MapBiomas Argentina Colección 3 (2016-2025, en píxeles) es
  de cerca del 0,7% de precisión para `cva`, 0,6% para `ndvi_diff`, 0% para
  `dynamic_world` y 5,8% para `veg_to_built` contra la ganancia urbana de
  MapBiomas (21 de 362 píxeles). Es una referencia débil: MapBiomas marca pocas
  hectáreas como leñosas y urbanas a 30 m, así que no describe la misma clase.
- `cva` está **sin calibrar**: su precisión se desconoce hasta que existan las
  etiquetas manuales.
- **Todavía no hay verdad de terreno.** El pipeline escribe una muestra
  estratificada (`manual_review_sample.csv`, 52 eventos). Cada `review_url` abre
  Google Earth Web sobre el evento; su control de imágenes históricas (ícono del
  reloj) permite comparar los veranos de `year_from` y `year_to`. Con la columna
  `label_true_positive` completa (1 = disminución real de cobertura leñosa,
  0 = falso positivo), `python -m gee_mh.validation --labels <csv>` informa la
  precisión por baseline y tamaño con intervalos de Wilson al 95%. Volver a
  correr `compare_baselines` nunca pisa una muestra con etiquetas.

### Serie larga, 1985-2026 (30 m)

Los compuestos de verano Landsat 5/7/8/9 (mediana de las escenas dentro de ±60
días del 15 de febrero, más ancha que la ventana de Sentinel-2 porque antes de
2000 hay pocas escenas) alimentan LandTrendr, que ajusta segmentos rectos por
píxel: una caída de un solo año que se recupera se suaviza, un escalón que se
queda permanece. Decisiones que cuidan los primeros años:

- **Al menos dos observaciones sin nube por píxel y año.** Una sola escena no
  puede descartar una nube o sombra que la máscara no vio, así que esos
  píxeles-año quedan vacíos y el ajuste interpola. `valid_fraction` en
  `longterm_rates.csv` es la fracción de la zona que cumple esto; está por debajo
  del 80% en 1988, 1990, 1992, 1993, 1994, 1995, 1996, 1999 y 2013 (1985 no tiene
  ninguna escena Landsat en la ventana).
- **Corrección de sensor.** El NDVI de Landsat 8/9 queda por encima del de
  Landsat 7 sobre vegetación (mediana 0,039 en el solapamiento 2014-2021, de
  -0,009 a 0,115 según el año), así que se le resta 0,04. Es una sola constante
  estimada con 8 años.
- **Persistencia y sequía.** La cobertura "arbórea" es NDVI ≥ 0,475, el umbral
  que mejor reproduce la clase árbol de Dynamic World a 30 m (F1 0,51, medido en
  todo el partido). Una pérdida cuenta solo si sigue ahí tres años después, y se
  mide contra el corrimiento regional de NDVI de ese año (también del partido
  completo).

El área construida sale de la clase urbana de MapBiomas: a 30 m el NDBI no separa
edificios de arena (F1 0,12 contra Dynamic World), así que no lo usamos.

| | 1986 | 2005 | 2009 | 2016 | 2023 | 2026 |
|---|---|---|---|---|---|---|
| Cobertura arbórea de la zona (ha) | 506 | 797 | 646 | 924 | 1.182 | 1.045 |
| Fracción de la zona | 10,9% | 17,1% | 13,9% | 19,8% | 25,4% | 22,5% |
| Fracción del resto del partido | 24,2% | 23,5% | 20,9% | 23,2% | 19,7% | 15,6% |

Dentro de la zona, la cobertura arbórea más que se duplicó entre 1986 y 2023:
creció casi todos los años, que es lo que parecen a 30 m la forestación de
médanos y los árboles urbanos al madurar, con una baja de unas 150 ha entre 2005
y 2009 (lluvia CHIRPS de septiembre a marzo de 343 mm en 2006 y 258 mm en 2009,
contra 450-500 mm en un año normal). De 2023 a 2026 perdió unas 137 ha (12%), y
el resto del partido perdió unas 668 ha (21%) en esos mismos años, así que la
baja reciente no es solo urbana. Los últimos tres años son provisorios porque el
chequeo de tres años queda cortado. La lluvia explica casi nada del cambio anual
(correlación de Spearman 0,05), y 2025-2026 fueron años húmedos.

**¿Permite ver qué pasó antes y después de la Ordenanza 1913 (2010)?** Permite
medir las tasas, pero con estos datos la diferencia no supera el ruido de un año
al otro. Antes = pares hasta 2010 (desde 1986), después = 2012-2026 (el par
2010-2011 cruza la sanción de la ordenanza). Promedio por año, con una prueba de
permutación y el efecto "después" ajustado por lluvia (intervalo bootstrap del
95%):

| Métrica (ha/año), todos los años | Antes | Después | p | Efecto después, ajustado por lluvia |
|---|---|---|---|---|
| Pérdida persistente de árboles | 1,5 | 4,6 | 0,24 | +3,1 (-1,6 a +9,8) |
| Cambio neto de cobertura arbórea | +6,4 | +23,0 | 0,10 | +16,6 (-6,3 a +35,8) |
| Crecimiento urbano (MapBiomas) | 3,5 | 1,1 | 0,09 | -2,4 (-4,5 a -0,6) |

Todos los intervalos incluyen el cero salvo el del crecimiento urbano en todos los
años, y ese pierde significancia al dejar afuera los años con menos de 80% de
datos válidos (2,6 contra 1,2 ha/año, p = 0,27), así que no lo leemos como un
efecto. La "ganancia de árboles" no está en la tabla porque es cero: la regla de
ganancia persistente pide un salto de 0,15 de NDVI en un año, y el reverdecer
gradual no lo produce, así que el cambio neto es el número que lo recoge. No hay
municipio de control, el reemplazo de tres por uno que exige la ordenanza no se
ve a 30 m (las copas de los árboles urbanos son menores que un píxel), y la zona
ya venía reverdeciendo décadas antes de 2010. Salidas: `longterm_rates.csv`,
`longterm_zone_vs_rest.csv`, `longterm_events.geojson`, `ordinance_summary.csv`.

### Contexto: todo el partido

Antes de definir la zona, el mismo pipeline corrió sobre todo el partido (208
km²) con un mínimo de 1.000 m², y sus salidas quedan en `data/exports/partido/`
(git no las sigue). Encontró 2.172 eventos de 2017 a 2026 (202, 108, 147, 185,
190, 115, 144, **429**, 48 y 604 sin confirmar), y su cobertura arbórea fue 4.461
ha (1986), 4.068 (2009), 4.710 (2016), 4.399 (2023) y 3.594 (2026). De ahí sale el
corrimiento regional de NDVI de arriba y la columna "resto del partido".

## Uso previsto de imágenes de Planet 🌍

Planeamos postular al programa Education and Research de Planet a través de una
afiliación universitaria. El pipeline ya funciona sin datos de Planet; la
imagen de alta resolución atacaría sus dos mayores límites, la resolución y la
verdad de terreno:

1. **Verdad de terreno.** Revisión visual de los eventos muestreados sobre
   imágenes de ~3 m para armar el conjunto etiquetado que hoy es el cuello de
   botella. Casos abiertos para resolver con ellas: el pico de 2024 sin explicar y
   la mancha de 31 ha de diciembre de 2025 junto a Balneario Sauce Grande.
2. **Eventos más chicos.** El evento detectado mediano es de unos 1.000 m²,
   cerca de 10 píxeles Sentinel-2 pero unos 110 píxeles a 3 m; una casa en un
   lote queda por debajo del mínimo de 500 m². Una zona de unos 46 km² también es
   un área manejable para imágenes que se piden por área.
3. **Calibración de copas.** Imágenes de menos de 10 m para calibrar el rango de
   escenarios de área a árboles en `trees.py`.
4. **Fechado y estacionalidad.** Revisitas frecuentes para fechar los eventos y
   separar la variación estacional del cambio permanente.

**Política de datos:** este repositorio no contiene, ni contendrá, imágenes de
Planet ni valores de píxeles crudos de Planet. Todo lo derivado de datos de
Planet seguirá las reglas de uso compartido del programa y llevará el aviso
"Image © 20xx Planet Labs PBC". Todos los resultados de arriba se produjeron sin
datos de Planet.

## Inicio rápido 🚀

```bash
pip install -r requirements.txt
earthengine authenticate
# configurá tu propio proyecto de Google Cloud en config.yaml -> ee_project

export PYTHONPATH=src
python3 -m gee_mh.aoi                 # descarga el límite del partido
python3 -m gee_mh.aoi --zona          # arma la zona de estudio (huella urbana + 1 km), data/aoi/zona_urbana.geojson
python3 -m gee_mh.compare_baselines --start-year 2016 --end-year 2026
python3 -m gee_mh.external_validation --full --start-year 2016 --end-year 2025
python3 -m gee_mh.validation --labels data/exports/manual_review_sample.csv
python3 -m gee_mh.longterm --start-year 1985 --end-year 2026   # serie Landsat, tasas, tabla de la ordenanza
python3 -m gee_mh.developments                                  # eventos cerca de los desarrollos conocidos
python3 -m gee_mh.animation                                      # docs/monte_hermoso.gif y datos del mapa web
```

Autochequeos sin conexión (no necesitan Earth Engine):
`python3 -m gee_mh.preprocessing`, `gee_mh.detection`, `gee_mh.export`,
`gee_mh.validation`, `gee_mh.attribution`, `gee_mh.aoi --selftest`,
`gee_mh.developments --selftest`, `gee_mh.longterm --selftest`,
`gee_mh.external_validation --selftest`.

## Estructura del repositorio 📦

| Módulo | Función |
|---|---|
| `preprocessing.py` | Descarga de Earth Engine, compuestos, índices, caché en disco |
| `timeseries.py` | Serie anual (en paralelo), con el año previo/siguiente como contexto |
| `masks.py`, `detection.py` | Máscaras de terreno, los cuatro baselines, corrimiento regional de NDVI, refinamiento (`detect_pair`) |
| `attribution.py`, `trees.py` | Causa por polígono, rango de escenarios de cantidad de árboles |
| `longterm.py` | Serie Landsat 1985-2026 con LandTrendr, tasas, antes/después de la ordenanza |
| `animation.py`, `docs/` | Cuadros anuales, GIF y mapa web estático (`docs/index.html`) |
| `data/reference/` | Desarrollos inmobiliarios públicos hallados en prensa y fuentes municipales |
| `export.py`, `validation.py` | GeoJSON/CSV/GeoTIFF, métricas, muestreo estratificado, precisión |
| `aoi.py`, `config.py` | Límite del partido, zona de estudio, carga de `config.yaml` |
| `developments.py` | Eventos cerca de los desarrollos inmobiliarios conocidos |
| `compare_baselines.py`, `external_validation.py`, `pipeline.py` | Puntos de entrada |

## Herramientas 🛠️

- Python, NumPy, pandas
- API de Python de Google Earth Engine
- GeoPandas, Shapely, rasterio
- OpenCV (componentes conectados, remuestreo, suavizado)
- PyYAML

## Habilidades 🧠

- Series temporales de teledetección: compuestos estacionales sin nubes con
  Sentinel-2 y Landsat
- Detección de cambios y análisis de falsos positivos: averiguar qué marca
  realmente un detector
- Ingeniería de datos consciente de la cuota en Earth Engine: descargas de un
  solo request, caché en disco, reintentos, checkpoints
- Diseño de validación: muestreo estratificado, intervalos de Wilson,
  contrastes independientes
- Código de investigación reproducible: autochequeos offline, reporte honesto
  de resultados negativos
- Licencias y atribución de datos abiertos

## Limitaciones ⚠️

- La cobertura leñosa incluye arbustos, forestación de médanos y plantaciones;
  ninguna capa libre separa el bosque natural de los árboles plantados.
- Un solo compuesto de verano por año puede confundir estrés o sequía con una
  disminución real de la cobertura leñosa.
- Con 10 m no se pueden contar árboles individuales; a 30 m, los árboles
  urbanos con copas menores que un píxel no se ven, así que la serie larga ve
  el monte de médanos y las plantaciones, no el arbolado de calle.
- La zona de estudio se dibujó desde la huella urbana de hoy, así que está
  sesgada hacia lugares que se desarrollaron; sus tasas no se pueden comparar con
  las del campo como si fuera un control. La parcela de Los Bosques sale de un PDF, con unos 250 m de error.
- La serie larga tiene pocas observaciones Landsat en la ventana en 9 años (ver
  `valid_fraction`) y 2012-2013 dependen de Landsat 7 (sensor con bandas): esos
  años se interpolan. La corrección de sensor entre Landsat 7 y 8/9 es una sola
  constante (0,04) estimada con 8 años de solapamiento. Los últimos tres años son
  provisorios.
- Las comparaciones antes y después no tienen municipio de control y no pueden
  mostrar que la ordenanza causó un cambio.
- La detección inmobiliaria depende de la clase construido de Dynamic World a
  10 m, que confunde arena clara con edificios; las causas son evidencia, no
  prueba.
- Las cifras de área anteriores a la corrección de polígonos venían de polígonos
  un píxel más chicos y contaban los huecos como eventos; las del partido
  completo de arriba ya usan los corregidos.
- La calidad de la detección no está medida hasta que exista la muestra
  etiquetada.

## Agradecimientos y licencia 📎

La idea y el diseño inicial vienen del proyecto de detección de cambios de uso
del suelo del
[capítulo Córdoba, Argentina](https://github.com/OmdenaAI/CordobaArgentinaChapter_MonitoringLandUseTransformation)
de Omdena; este repositorio no incluye archivos de ese proyecto. Ver
[NOTICE](NOTICE). Publicado bajo licencia MIT ([LICENSE](LICENSE)).
