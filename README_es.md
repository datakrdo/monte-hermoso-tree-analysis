🇬🇧 [English](README.md) · 🇪🇸 [Español](README_es.md)

# Análisis de la cobertura arbórea en Monte Hermoso 🌳🛰️

Un pipeline abierto y reproducible que detecta el cambio anual de vegetación
leñosa (árboles y arbustos) en el partido de **Monte Hermoso**, un área costera
del sur de la provincia de Buenos Aires, Argentina, usando **solo datos libres
y públicos** procesados en Google Earth Engine.

> **Estado: prototipo de investigación.** El pipeline está resuelto, es rápido
> y reproducible, pero la precisión de la detección **todavía no está
> validada**: la revisión manual que la mide es hoy el cuello de botella (ver
> [Estado actual](#estado-actual-y-validación-)).

Cada evento detectado es un **cambio candidato** en la señal satelital. El
proyecto no da por hecho que sea una pérdida de árboles: la revisión de una
muestra etiquetada es lo que dice cuáles lo son.

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

1. Producir una serie anual (2000 a hoy) de eventos de cambio de cobertura
   leñosa para todo el partido, en GeoJSON y CSV, con superficie en m² y ha.
2. Usar solo datos libres y gastar la mínima cuota de Earth Engine, para que
   cualquier municipio, escuela u ONG pueda repetirlo.
3. Medir con honestidad qué tan preciso es: revisión visual estratificada de
   los eventos detectados, más contraste con productos públicos independientes.
4. Informar los árboles como un **rango/escenario derivado de la superficie
   con cambio detectado**, nunca como un conteo observado (con píxeles de 10 m no se
   distinguen copas individuales).
5. Atribuir causas (desarrollo urbano vs. natural) solo cuando la evidencia lo
   respalda; si no, informar `desconocido`.

## Enfoque: solo datos libres y abiertos 🛰️

| Uso | Dataset (id en Earth Engine) | Licencia | Crédito |
|---|---|---|---|
| Imágenes desde 2015 (10 m) | Sentinel-2 SR Harmonized (`COPERNICUS/S2_SR_HARMONIZED`) | Términos de datos Copernicus Sentinel | Contains modified Copernicus Sentinel data |
| Imágenes 2000-2014 y años sin cobertura (30 m) | Landsat 5/7/8/9 Collection 2 Level-2 | Dominio público (USGS) | Courtesy U.S. Geological Survey |
| Máscara de nubes | Cloud Score+ (`GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED`) | CC BY 4.0 | Pasquarella et al. 2023 |
| Cobertura previa y baseline (desde 2015) | Dynamic World v1 (`GOOGLE/DYNAMICWORLD/V1`) | CC BY 4.0 | Brown et al. 2022 |
| Contraste independiente | MapBiomas Pampa Colección 4 | Términos de MapBiomas (CC BY-SA; sus páginas son inconsistentes) | Project MapBiomas Trinational Pampa |
| Contraste independiente | Hansen Global Forest Change (`UMD/hansen/global_forest_change_2025_v1_13`) | CC BY 4.0 | Hansen et al. 2013 |
| Límite del partido | IGN Argentina vía Georef (datos.gob.ar) | CC BY 4.0 | Servicio Georef – argentina.gob.ar/georef (modificado: campos agregados) |

Las citas completas están en [NOTICE](NOTICE).

## Pipeline 🏗️

```
Earth Engine: Sentinel-2 / Landsat / Dynamic World / Cloud Score+
   |  1 request por año: compuesto de verano sin nubes (mediana, +-30 días del 15-feb)
   v
caché en disco (data/cache)  -->  SatelliteImage: reflectancia, NDVI/NDBI/NDMI,
   |                              etiqueta de Dynamic World y probabilidad de árbol
   v
detección (3 baselines: ndvi_diff | dynamic_world | cva), cada uno refinado con
   - máscara leñosa estable (leñosa en el año t-1 y t)
   - persistencia (la disminución debe mantenerse en el año t+1)
   - recorte al polígono del partido, área mínima del evento
   v
events.geojson  -->  muestra estratificada + review_url  -->  precisión por estrato
   +-- contraste con MapBiomas / Hansen
```

## Notas de ingeniería 🧭

- **Una descarga por año**, no por tile: grilla exacta con `computePixels`
  (bandas crudas en `uint16`, una etiqueta de cobertura `uint8` y una
  probabilidad de árbol `uint8`). Una corrida completa solía tardar unos 30
  minutos y fallaba por un desajuste de tamaño entre tiles; ahora las 10
  imágenes de 2015-2024 bajan en unos 2,5 minutos en total (medido), y una nueva
  corrida sale de la caché en disco sin pedir imágenes a Earth Engine.
- Reintentos con espera creciente, timeouts y un checkpoint tras cada año
  procesado: un fallo no pierde el trabajo ya terminado.
- Los años corren en paralelo (4 hilos, conservador para la cuota no comercial).
- Los pares de años con distinta resolución de sensor (30 m vs. 10 m) se
  omiten, porque el NDVI no es comparable entre ellos.

## Estado actual y validación 📊

Medido en 2016-2023:

| Etapa de refinamiento | Eventos candidatos (`ndvi_diff` + `dynamic_world`) |
|---|---|
| Caída de NDVI / transición de clase simple (82% dentro del partido) | 28.839 |
| + máscara leñosa estable, persistencia, recorte al partido (100% dentro), área mínima | 4.774 |
| + compuestos de 60 días sin nubes, baseline por probabilidad de árbol | 1.644 |

El tercer baseline, `cva` (un Change Vector Analysis mínimo con umbral
estadístico), suma 897 eventos: 2.541 en total, todos dentro del partido.

Menos candidatos **no** significa por sí solo mayor precisión. Lo que sabemos:

- El diagnóstico inicial mostró que la mayor parte de lo que marcaba una caída
  simple de NDVI era pasto o cultivo cambiando con la estación, no pérdida de
  árboles; las etapas de arriba apuntan justo a eso.
- La coincidencia con MapBiomas ronda el 1-2% de precisión en los tres
  baselines. Acá es una referencia débil: MapBiomas marca cerca del 4% del
  partido como leñoso al inicio de un par y Dynamic World entre 43% y 51%
  (cuenta el arbustal), así que no describen la misma clase. Se usa solo como
  indicador de tendencia.
- Una variante anterior de `cva`, calibrada contra Dynamic World, no encontró
  eventos sobre el partido completo. Se reemplazó por la versión más simple de
  arriba, que está **sin calibrar**: su precisión se desconoce hasta que
  existan las etiquetas manuales.
- La atribución de causa hoy devuelve `desconocido` para todos los eventos.
- La serie 2000-2014 (solo Landsat) está soportada por el código pero no se
  corrió de punta a punta, y no tiene la cobertura previa de Dynamic World.
- **Todavía no hay verdad de terreno.** El pipeline escribe una muestra
  estratificada (`manual_review_sample.csv`, 45 eventos, con un `review_url` por
  evento). Con la columna `label_true_positive` completa (1 = disminución real de cobertura leñosa,
  0 = falso positivo), `python -m gee_mh.validation --labels <csv>` informa la
  precisión por baseline y tamaño con intervalos de Wilson al 95%.

## Uso previsto de imágenes de Planet 🌍

Planeamos postular al programa Education and Research de Planet a través de una
afiliación universitaria. El pipeline ya funciona sin datos de Planet; la
imagen de alta resolución atacaría sus dos mayores límites, la resolución y la
verdad de terreno:

1. **Verdad de terreno.** Revisión visual de los eventos muestreados sobre
   imágenes de ~3 m para construir el conjunto etiquetado que hoy es el cuello
   de botella.
2. **Eventos más chicos.** El evento mediano detectado ronda los 850 m²: unos 8
   píxeles de Sentinel-2, pero unos 90 píxeles a 3 m.
3. **Calibración por copa.** Imagen de menos de 10 m para calibrar el rango de
   árboles estimados a partir de la superficie (`trees.py`).
4. **Fechado y estacionalidad.** Revisitas frecuentes para fechar los eventos y
   separar la variación estacional del cambio permanente.

**Política de datos:** este repositorio no contiene ni contendrá imágenes de
Planet ni valores de píxel crudos de Planet. Todo lo derivado de datos de Planet
seguirá las reglas de uso del programa y llevará el aviso
"Image © 20xx Planet Labs PBC". Todos los resultados de arriba se obtuvieron
sin datos de Planet.

## Inicio rápido 🚀

```bash
pip install -r requirements.txt
earthengine authenticate
# configurá tu propio proyecto de Google Cloud en config.yaml -> ee_project

export PYTHONPATH=src
python3 -m gee_mh.aoi                 # descarga el límite del partido
python3 -m gee_mh.compare_baselines --start-year 2016 --end-year 2023
python3 -m gee_mh.external_validation --full --start-year 2016 --end-year 2023
python3 -m gee_mh.validation --labels data/exports/manual_review_sample.csv
```

Autochequeos offline (sin Earth Engine):
`python3 -m gee_mh.preprocessing`, `gee_mh.detection`, `gee_mh.export`,
`gee_mh.validation`, `gee_mh.external_validation --selftest`.

## Estructura del repositorio 📦

| Módulo | Función |
|---|---|
| `preprocessing.py` | Descarga de Earth Engine, compuestos, índices, caché en disco |
| `timeseries.py` | Serie anual (en paralelo), con año previo/siguiente como contexto |
| `masks.py`, `detection.py` | Máscaras de suelo, los tres baselines, refinamiento (`detect_pair`) |
| `attribution.py`, `trees.py` | Heurística de causa, rango de árboles estimados |
| `export.py`, `validation.py` | GeoJSON/CSV/GeoTIFF, métricas, muestreo estratificado, precisión |
| `aoi.py`, `config.py` | Descarga del límite del partido, lectura de `config.yaml` |
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
  no hay una capa libre que separe bosque natural de árboles plantados.
- Un único compuesto de verano por año todavía puede confundir estrés o sequía
  con una disminución real de cobertura leñosa.
- Con 10 m no se pueden contar árboles individuales.
- La calidad de la detección no está medida hasta que exista la muestra
  etiquetada.

## Agradecimientos y licencia 📎

La idea y el diseño inicial vienen del proyecto de detección de cambios de uso
del suelo del
[capítulo Córdoba, Argentina](https://github.com/OmdenaAI/CordobaArgentinaChapter_MonitoringLandUseTransformation)
de Omdena; este repositorio no incluye archivos de ese proyecto. Ver
[NOTICE](NOTICE). Publicado bajo licencia MIT ([LICENSE](LICENSE)).
