from typing import List
from enum import Enum
import numpy
import cv2
import ee
import hashlib
import json
import math
import os
import sys
import threading
import time
from pathlib import Path

from gee_mh.config import PROJECT_ROOT

# Dynamic World classes, in the order of the class values of its 'label' band
DYNAMIC_WORLD_CLASSES = [
    "water", "trees", "grass", "flooded_vegetation", "crops",
    "shrub_and_scrub", "built", "bare", "snow_and_ice",
]

# Value of the Dynamic World label where there is no Dynamic World data
DW_NO_DATA = 255

# Raw bands downloaded from Earth Engine (common names)
RAW_BANDS = ["red", "nir", "swir"]

# Bump when anything that changes the downloaded data changes (bands, scale,
# collections, grid), to invalidate the disk cache
CACHE_VERSION = 2
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# Cloud Score+ (Sentinel-2): pixels with cs_cdf below this are masked
CLOUD_SCORE_PLUS = "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"
CLOUD_SCORE_MIN = 0.6

# Max number of pixels per computePixels request (8 bytes/pixel for the raw
# bands + dynamic world label and trees probability: stays well under the
# 48MB request limit)
MAX_PIXELS_PER_REQUEST = 4_000_000

METERS_PER_DEGREE_LAT = 111_320.0

# Composites drop scenes with more cloud cover than this (percent)
DEFAULT_MAX_CLOUD_PERCENT = 35.0

# Gaussian smoothing applied to the raw bands (radius in pixels; 0 disables it)
DEFAULT_BLUR = {"radius": 3, "sigma": 0.5}

class DataSource(Enum):
    """Imagery collections the pipeline can read from."""
    SENTINEL2 = 0
    LANDSAT8 = 1  # also covers Landsat 9 (same bands and scaling)
    LANDSAT5 = 2
    AUTO = 3      # try SEARCH_ORDER and keep the first source with imagery
    LANDSAT7 = 4

    def __str__(self):
        return self.name

# Per source: Earth Engine collections, raw bands (red, nir, swir1), cloud
# property, native resolution (meter per pixel), and the reflectance scaling
# (reflectance = DN * scale + offset). Landsat 5/7 have the same band layout
# (SWIR1 is SR_B5), Landsat 8/9 are shifted by one band.
SOURCES = {
    DataSource.SENTINEL2: {
        "collections": ["COPERNICUS/S2_SR_HARMONIZED"],
        "bands": ["B4", "B8", "B11"],
        "cloud": "CLOUDY_PIXEL_PERCENTAGE",
        "resolution": 10.0, "scale": 1e-4, "offset": 0.0,
    },
    DataSource.LANDSAT8: {
        "collections": ["LANDSAT/LC08/C02/T1_L2", "LANDSAT/LC09/C02/T1_L2"],
        "bands": ["SR_B4", "SR_B5", "SR_B6"],
        "cloud": "CLOUD_COVER",
        "resolution": 30.0, "scale": 2.75e-5, "offset": -0.2,
    },
    DataSource.LANDSAT5: {
        "collections": ["LANDSAT/LT05/C02/T1_L2"],
        "bands": ["SR_B3", "SR_B4", "SR_B5"],
        "cloud": "CLOUD_COVER",
        "resolution": 30.0, "scale": 2.75e-5, "offset": -0.2,
    },
    DataSource.LANDSAT7: {
        "collections": ["LANDSAT/LE07/C02/T1_L2"],
        "bands": ["SR_B3", "SR_B4", "SR_B5"],
        "cloud": "CLOUD_COVER",
        "resolution": 30.0, "scale": 2.75e-5, "offset": -0.2,
    },
}
# Landsat 7 is a last resort (SLC-off stripes since 2003)
SEARCH_ORDER = [DataSource.SENTINEL2, DataSource.LANDSAT8, DataSource.LANDSAT5, DataSource.LANDSAT7]

class LongLatBBox:
    """Axis-aligned box in WGS84 degrees."""

    def __init__(self, long_from: float, long_to: float, lat_from: float, lat_to: float):
        self.long_from = long_from
        self.long_to = long_to
        self.lat_from = lat_from
        self.lat_to = lat_to

    def to_ee_box(self) -> ee.Geometry.Rectangle:
        west, south, east, north = self.long_from, self.lat_from, self.long_to, self.lat_to
        return ee.Geometry.Rectangle([west, south, east, north])

    def __str__(self):
        return "lo[%s,%s],la[%s,%s]" % (self.long_from, self.long_to, self.lat_from, self.lat_to)

def grid_size(area: LongLatBBox, resolution: float) -> tuple:
    """
    Dimensions (width, height) in pixels of the grid covering the area at the
    given resolution (meter per pixel). The grid spans the area exactly, so
    pixel (x, y) maps linearly to longitude/latitude.
    """
    lat_mid = (area.lat_from + area.lat_to) / 2.0
    meters_per_degree_long = METERS_PER_DEGREE_LAT * math.cos(math.radians(lat_mid))
    width = max(1, round((area.long_to - area.long_from) * meters_per_degree_long / resolution))
    height = max(1, round((area.lat_to - area.lat_from) * METERS_PER_DEGREE_LAT / resolution))
    return width, height

def _normalized_difference(a: numpy.ndarray, b: numpy.ndarray, valid: numpy.ndarray) -> numpy.ndarray:
    # NOTE: pixels without data get 0.0 (as the NPY export did before),
    # which a following year-to-year difference can read as a change.
    total = a + b
    out = numpy.zeros_like(total)
    numpy.divide(a - b, total, out=out, where=valid & (total != 0))
    return numpy.clip(out, -1.0, 1.0)

class SatelliteImage:
    """One annual composite on a regular grid: spectral bands and indices,
    plus the Dynamic World layers when they exist for that year."""

    def __init__(self, date: str, area: LongLatBBox, resolution: float, width: int, height: int):
        self.date = date
        self.area = area
        self.resolution = resolution  # meters per pixel
        self.width = width
        self.height = height
        self.source = None
        # band name -> 2D array (reflectance bands and the ndvi/ndbi/ndmi indices)
        self.bands = {}
        # Dynamic World most frequent class per pixel (index into
        # DYNAMIC_WORLD_CLASSES, DW_NO_DATA where missing); None before 2015-06-27
        self.dw_label = None
        # Dynamic World mean probability of 'trees' (0-100); None before 2015-06-27
        self.dw_trees_p = None
        self.mean_ndvi = 0.0

    def __str__(self):
        return f"{self.date} {self.source} {self.width}x{self.height}px at {self.resolution}m"

    def to_dynamic_world_mask(self, class_name: str) -> numpy.ndarray:
        """Boolean mask of the pixels whose Dynamic World class is class_name."""
        return self.dw_label == DYNAMIC_WORLD_CLASSES.index(class_name)


def mask_landsat_clouds(image: ee.Image) -> ee.Image:
    # QA_PIXEL (Collection 2): bit 1 dilated cloud, 2 cirrus (Landsat 8/9
    # only, always 0 otherwise), 3 cloud, 4 cloud shadow
    return image.updateMask(image.select('QA_PIXEL').bitwiseAnd(0b11110).eq(0))

class Preprocessor:
    """
    Downloads satellite data from Earth Engine (one request per image, cached
    on disk) and turns it into SatelliteImage objects
    """

    def __init__(self, ee_project=None, online=True, cache_dir=DEFAULT_CACHE_DIR):
        """
        ee_project: Google Cloud project linked to your Earth Engine account
        (log in first with `earthengine authenticate`).
        online: False builds zero-filled dummy images and never calls Earth
        Engine (used by the offline self-checks).
        cache_dir: where downloaded composites are stored (None disables it).
        """
        self.online = online

        # Uses the credentials of whoever ran `earthengine authenticate`.
        if online:
            ee.Initialize(project=ee_project)
            # Sin esto, una llamada de la API que se cuelga a nivel TCP nunca
            # falla, y por lo tanto nunca dispara el reintento de
            # download_numpy_data. 120s porque un computePixels del AOI
            # completo (mediana de varias imágenes) tarda más que un tile.
            ee.data.setDeadline(120000)

        self.cache_dir = Path(cache_dir) if cache_dir is not None else None

        self.data_source = DataSource.AUTO
        self.max_cloud_coverage = DEFAULT_MAX_CLOUD_PERCENT

        # Resolution for dummy images and for downloads that do not say
        # otherwise; real composites use the native resolution of their
        # source (see SOURCES).
        self.resolution = SOURCES[DataSource.LANDSAT8]["resolution"]

        self.flag_verbose = True
        self.gaussian_blur = dict(DEFAULT_BLUR)

        # The composite is the median of all the images within +-30 days of
        # the date (step_search_image * nb_max_step_search): a wider window
        # than a single scene averages out clouds, shadows and sun-angle
        # noise, and makes Dynamic World's most-frequent label more stable.
        self.step_search_image = 30
        self.nb_max_step_search = 1

        # Mask clouds (Cloud Score+ for Sentinel-2, QA_PIXEL for Landsat)
        # before the median
        self.flag_cloud_filtering = True

    def _cache_file(self, *key_parts):
        if self.cache_dir is None:
            return None
        key = "|".join(map(str, key_parts + (CACHE_VERSION,)))
        return self.cache_dir / (hashlib.md5(key.encode()).hexdigest()[:20] + ".npz")

    def _cache_read(self, path):
        """Return (data, meta) or None if there is no cached file."""
        if path is None or not path.exists():
            return None
        with numpy.load(path) as cached:
            return cached["data"], json.loads(str(cached["meta"]))

    def _cache_write(self, path, data, meta):
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file first: concurrent threads/crashes never
        # leave a partial file in the cache.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp.npz")
        numpy.savez_compressed(tmp, data=data, meta=json.dumps(meta))
        os.replace(tmp, path)

    def cached(self, key_parts: tuple, download):
        """
        Return the data cached under key_parts, or call download() and cache
        its result.
        """
        path = self._cache_file("data", *key_parts)
        hit = self._cache_read(path)
        if hit is not None:
            return hit[0]
        data = download()
        self._cache_write(path, data, {})
        return data

    def _collection(self, source: DataSource, area_bounding: ee.Geometry) -> ee.ImageCollection:
        """
        Image collection of a source over the area, without too many clouds
        """
        spec = SOURCES[source]
        collection = ee.ImageCollection(spec["collections"][0])
        for extra in spec["collections"][1:]:
            collection = collection.merge(ee.ImageCollection(extra))
        return collection \
            .filterBounds(area_bounding) \
            .filter(ee.Filter.lt(spec["cloud"], self.max_cloud_coverage))

    def _dynamic_world(self, area_bounding: ee.Geometry, center: ee.Date, shift_day: int) -> ee.ImageCollection:
        # Dynamic World only covers imagery from 2015-06-27 onward, so for
        # earlier dates the collection is empty.
        return ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1") \
            .filterBounds(area_bounding) \
            .filterDate(center.advance(-shift_day, "day"), center.advance(shift_day, "day"))

    def _find_source_window(self, date: str, area: LongLatBBox):
        """
        Search the first source (in SEARCH_ORDER if the data source is AUTO)
        with at least one image in a window around the date, widening the
        window step by step. All the counts are fetched with one request.
        Return (source, shift_day, has_dynamic_world), or None if there is no
        image available.
        """
        area_bounding = area.to_ee_box()
        center = ee.Date(date)
        shifts = [self.step_search_image * k for k in range(1, self.nb_max_step_search + 1)]
        sources = SEARCH_ORDER if self.data_source == DataSource.AUTO else [self.data_source]

        sizes = {}
        for source in sources:
            collection = self._collection(source, area_bounding)
            for shift in shifts:
                sizes[f"{source.name}_{shift}"] = collection.filterDate(
                    center.advance(-shift, "day"), center.advance(shift, "day")).size()
        for shift in shifts:
            sizes[f"DW_{shift}"] = self._dynamic_world(area_bounding, center, shift).size()
        counts = ee.Dictionary(sizes).getInfo()

        for source in sources:
            for shift in shifts:
                if counts[f"{source.name}_{shift}"] > 0:
                    return source, shift, counts[f"DW_{shift}"] > 0
        return None

    def _composite(self, date: str, area: LongLatBBox, source: DataSource, shift_day: int, with_dw: bool) -> ee.Image:
        """
        Median composite of the source images around the date, as a uint16
        image with the RAW_BANDS (+ the uint8 Dynamic World label as band
        'dw' when with_dw).
        """
        area_bounding = area.to_ee_box()
        center = ee.Date(date)
        collection = self._collection(source, area_bounding).filterDate(
            center.advance(-shift_day, "day"), center.advance(shift_day, "day"))
        if self.flag_cloud_filtering:
            if source == DataSource.SENTINEL2:
                # Cloud Score+ instead of QA60, which is not reliable (empty
                # for part of 2022-2024)
                collection = collection \
                    .linkCollection(ee.ImageCollection(CLOUD_SCORE_PLUS), ["cs_cdf"]) \
                    .map(lambda image: image.updateMask(image.select("cs_cdf").gte(CLOUD_SCORE_MIN)))
            else:
                collection = collection.map(mask_landsat_clouds)
        ee_image = collection.median().select(SOURCES[source]["bands"], RAW_BANDS).toUint16()
        if with_dw:
            dynamic_world = self._dynamic_world(area_bounding, center, shift_day)
            # Most frequent label, and mean probability of 'trees' (x100)
            ee_dw = dynamic_world.select("label").mode().unmask(DW_NO_DATA).toUint8().rename("dw")
            ee_trees = dynamic_world.select("trees").mean().multiply(100).unmask(0).toUint8().rename("dw_trees_p")
            ee_image = ee_image.addBands([ee_dw, ee_trees])
        return ee_image

    def get_dummy_image(self, date: str, area: LongLatBBox) -> SatelliteImage:
        """Zero-filled stand-in with the grid a real download of `area` would have."""
        width, height = grid_size(area, self.resolution)
        image = SatelliteImage(date, area, self.resolution, width, height)
        for name in RAW_BANDS + ["ndvi", "ndbi", "ndmi"]:
            image.bands[name] = numpy.zeros((height, width))
        return image

    def get_satellite_data(self, dates: List[str], area: LongLatBBox) -> List[SatelliteImage]:
        """One SatelliteImage per date ("YYYY-MM-DD"); a zero-filled dummy when a date has no imagery."""
        images = []
        for date in dates:
            image = self._get_satellite_image(date, area) if self.online else None
            images.append(image if image is not None else self.get_dummy_image(date, area))
        return images

    def _get_satellite_image(self, date: str, area: LongLatBBox):
        """
        Download (or read from the disk cache) the data of one date, and
        convert it into a SatelliteImage. None if no data is available.
        """
        cache_path = self._cache_file(
            "satellite", date, area, self.data_source, self.max_cloud_coverage,
            self.step_search_image, self.nb_max_step_search)
        hit = self._cache_read(cache_path)
        if hit is not None:
            raw, meta = hit
            if self.flag_verbose:
                print(f"cache hit: {date}")
                sys.stdout.flush()
            return self._build_image(date, area, raw, DataSource[meta["source"]], meta["resolution"])

        found = self._find_source_window(date, area)
        if found is None:
            return None
        source, shift_day, with_dw = found
        if self.flag_verbose:
            print(f"{date}: data source: {source} (+-{shift_day} days, dynamic world: {with_dw})")
            sys.stdout.flush()

        resolution = SOURCES[source]["resolution"]
        ee_image = self._composite(date, area, source, shift_day, with_dw)
        raw = self.download_numpy_data(
            ee_image, area, RAW_BANDS + (["dw", "dw_trees_p"] if with_dw else []), resolution)
        self._cache_write(cache_path, raw, {"source": source.name, "resolution": resolution})
        return self._build_image(date, area, raw, source, resolution)

    def _build_image(self, date: str, area: LongLatBBox, raw: numpy.ndarray, source: DataSource, resolution: float) -> SatelliteImage:
        """
        Convert the raw data of a download (digital numbers) into a
        SatelliteImage: gaussian blur, reflectance and spectral indices.
        """
        height, width = raw.shape
        image = SatelliteImage(date, area, resolution, width, height)
        image.source = source
        spec = SOURCES[source]

        valid = (raw["red"] > 0) & (raw["nir"] > 0) & (raw["swir"] > 0)
        radius = self.gaussian_blur["radius"]
        for name in RAW_BANDS:
            band = raw[name].astype(numpy.float32)
            if radius > 0:
                kernel_size = 2 * radius + 1
                band = cv2.GaussianBlur(band, (kernel_size, kernel_size), self.gaussian_blur["sigma"])
            image.bands[name] = band * spec["scale"] + spec["offset"]

        red, nir, swir = (image.bands[name] for name in RAW_BANDS)
        image.bands["ndvi"] = _normalized_difference(nir, red, valid)
        image.bands["ndbi"] = _normalized_difference(swir, nir, valid)
        image.bands["ndmi"] = _normalized_difference(nir, swir, valid)

        if "dw" in raw.dtype.names:
            image.dw_label = raw["dw"]
            image.dw_trees_p = raw["dw_trees_p"]
        image.mean_ndvi = float(image.bands["ndvi"].mean())
        return image

    def download_numpy_data(self, ee_image: ee.Image, area: LongLatBBox, bands_name: List[str], resolution: float=None) -> numpy.array:
        """
        Download bands_name of ee_image over area as a structured array of
        shape (height, width) with one field per band. The grid spans the area
        exactly; large areas go out in row chunks, one request each.
        resolution: meters per pixel (default self.resolution).
        """
        resolution = resolution or self.resolution
        width, height = grid_size(area, resolution)
        dx = (area.long_to - area.long_from) / width
        dy = (area.lat_to - area.lat_from) / height
        rows_per_request = max(1, MAX_PIXELS_PER_REQUEST // width)

        chunks = []
        for row in range(0, height, rows_per_request):
            rows = min(rows_per_request, height - row)
            request = {
                "expression": ee_image,
                "fileFormat": "NUMPY_NDARRAY",
                "bandIds": bands_name,
                "grid": {
                    "dimensions": {"width": width, "height": rows},
                    "affineTransform": {
                        "scaleX": dx, "shearX": 0, "translateX": area.long_from,
                        "shearY": 0, "scaleY": -dy, "translateY": area.lat_to - row * dy},
                    "crsCode": "EPSG:4326",
                },
            }
            chunks.append(self._compute_pixels(request, area, (rows, width)))
        return chunks[0] if len(chunks) == 1 else numpy.vstack(chunks)

    def _compute_pixels(self, request: dict, area: LongLatBBox, expected_shape: tuple) -> numpy.array:
        if self.flag_verbose:
            print(f"download...({expected_shape[1]}x{expected_shape[0]}px, from lat {request['grid']['affineTransform']['translateY']})")
            sys.stdout.flush()
        max_attempts = 3
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                data = ee.data.computePixels(request)
                if data.shape != expected_shape:
                    raise ValueError(f"shape {data.shape}, expected {expected_shape}")
                return data
            except Exception as exc:
                last_exc = exc
                print(f"download failed for area {area} (attempt {attempt}/{max_attempts})...\n{exc}")
                sys.stdout.flush()
                if attempt < max_attempts:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"download_numpy_data: giving up on area {area} after {max_attempts} attempts") from last_exc


def _selftest():
    """Self-check offline (computePixels is mocked, no EE calls): grid chunking
    and retry of download_numpy_data, and reflectance/indices of _build_image."""
    global MAX_PIXELS_PER_REQUEST
    processor = Preprocessor(online=False, cache_dir=None)
    processor.flag_verbose = False
    area = LongLatBBox(-61.4, -61.3, -39.0, -38.9)
    width, height = grid_size(area, 30.0)

    calls = []

    def flaky_compute(request):
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError("transient failure")
        w, h = request["grid"]["dimensions"]["width"], request["grid"]["dimensions"]["height"]
        transform = request["grid"]["affineTransform"]
        first_row = round((area.lat_to - transform["translateY"]) / -transform["scaleY"])
        data = numpy.zeros((h, w), dtype=[("red", "<u2")])
        data["red"] = (numpy.arange(h) + first_row)[:, None]  # global row index
        return data

    orig_compute, orig_sleep, orig_max_pixels = ee.data.computePixels, time.sleep, MAX_PIXELS_PER_REQUEST
    ee.data.computePixels, time.sleep = flaky_compute, lambda _s: None
    MAX_PIXELS_PER_REQUEST = width * 40  # 40 rows per request -> several chunks
    try:
        data = processor.download_numpy_data(object(), area, ["red"], 30.0)
        assert data.shape == (height, width), data.shape
        assert (data["red"][:, 0] == numpy.arange(height)).all(), "chunks not stacked north to south"
        assert len(calls) == 1 + math.ceil(height / 40), len(calls)  # 1 failed attempt + chunks

        ee.data.computePixels = lambda request: numpy.zeros((3, 3), dtype=[("red", "<u2")])
        try:
            processor.download_numpy_data(object(), area, ["red"], 30.0)
            raise AssertionError("expected RuntimeError on shape mismatch")
        except RuntimeError as exc:
            assert "giving up" in str(exc), exc
    finally:
        ee.data.computePixels, time.sleep, MAX_PIXELS_PER_REQUEST = orig_compute, orig_sleep, orig_max_pixels

    processor.gaussian_blur = {"radius": 0, "sigma": 0.5}
    raw = numpy.zeros((2, 2), dtype=[("red", "<u2"), ("nir", "<u2"), ("swir", "<u2"), ("dw", "u1"), ("dw_trees_p", "u1")])
    raw["dw"] = DW_NO_DATA
    raw["dw_trees_p"][0, 0] = 87
    raw["dw"][0, 0] = DYNAMIC_WORLD_CLASSES.index("trees")

    raw["red"][0, 0], raw["nir"][0, 0], raw["swir"][0, 0] = 2000, 6000, 4000
    image = processor._build_image("2020-02-15", area, raw, DataSource.SENTINEL2, 10.0)
    assert abs(image.bands["ndvi"][0, 0] - 0.5) < 1e-6, image.bands["ndvi"][0, 0]
    assert image.bands["ndvi"][1, 1] == 0.0  # no data
    assert image.to_dynamic_world_mask("trees")[0, 0] and not image.to_dynamic_world_mask("water").any()
    assert image.dw_trees_p[0, 0] == 87

    # Landsat C2: reflectance = DN * 2.75e-5 - 0.2 -> red 0.1, nir 0.3, ndvi 0.5
    # (0.25 if the offset were ignored)
    raw["red"][0, 0], raw["nir"][0, 0], raw["swir"][0, 0] = 10909, 18182, 14545
    image = processor._build_image("2005-02-15", area, raw, DataSource.LANDSAT5, 30.0)
    assert abs(image.bands["ndvi"][0, 0] - 0.5) < 1e-3, image.bands["ndvi"][0, 0]
    assert abs(image.bands["ndmi"][0, 0] - 0.2) < 1e-3, image.bands["ndmi"][0, 0]  # swir != nir

    print("preprocessing selftest OK")

if __name__ == "__main__":
    _selftest()
