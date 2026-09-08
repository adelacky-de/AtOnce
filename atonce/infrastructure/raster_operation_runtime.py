"""Raster CRS conversion and GeoTIFF/KMZ staging for the free-form canvas.

Vector feature lineage does not apply. Intermediate results are GeoTIFF files
loaded as QgsRasterLayer; KMZ packaging is a separate delivery path.

QGIS imports are lazy so stage_geotiff / stage_raster_kmz can be unit-tested
with mocked GDAL outside the QGIS Python environment.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Optional, Tuple
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape


class RasterOperationRuntime:
    """GDAL-backed raster adapters used by free-form raster SOURCE chains."""

    def __init__(self, project: Optional[Any] = None, layer_runtime=None):
        if project is None:
            from qgis.core import QgsProject

            project = QgsProject.instance()
        self.project = project
        self.layers = layer_runtime

    @staticmethod
    def _gdal():
        try:
            from osgeo import gdal  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Raster operations require GDAL (osgeo.gdal) inside the QGIS Python environment."
            ) from exc
        return gdal

    @staticmethod
    def _source_path(layer) -> str:
        if layer is None:
            raise ValueError("A raster layer is required.")
        path = str(getattr(layer, "source", lambda: "")() or "")
        if not path:
            raise ValueError("Raster layer has no readable source path.")
        # QGIS may append layer options after "|"; GDAL wants the file path.
        return path.split("|", 1)[0]

    def _load_raster(self, path: str, display_name: str):
        from qgis.core import QgsRasterLayer

        layer = QgsRasterLayer(path, display_name)
        if not layer.isValid():
            raise RuntimeError(f"Could not load raster result: {path}")
        # Do not addMapLayer here — graph install owns project membership once.
        return layer

    def build_reprojected_raster_layer(
        self,
        workflow_id: str,
        node_id: str,
        display_name: str,
        source_layer,
        parameters,
    ) -> Tuple[object, int]:
        from qgis.core import QgsCoordinateReferenceSystem

        gdal = self._gdal()
        target_crs = str((parameters or {}).get("target_crs") or "").strip()
        crs = QgsCoordinateReferenceSystem(target_crs)
        if not crs.isValid():
            raise ValueError(f"Invalid target CRS: {target_crs}")
        source_path = self._source_path(source_layer)
        dest = Path(self.project.homePath() or Path.cwd()) / ".atonce-raster"
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / f"{workflow_id[:8]}_{node_id.replace(':', '_')}_{uuid4().hex[:8]}.tif"
        options = gdal.WarpOptions(dstSRS=crs.authid() or target_crs)
        result = gdal.Warp(str(out_path), source_path, options=options)
        if result is None:
            raise RuntimeError(f"GDAL Warp failed for {source_path} → {target_crs}")
        result = None
        layer = self._load_raster(str(out_path), display_name or "Raster reproject")
        return layer, 1

    def build_converted_raster_layer(
        self,
        workflow_id: str,
        node_id: str,
        display_name: str,
        source_layer,
        parameters,
    ) -> Tuple[object, int]:
        """Normalize to GeoTIFF for chaining; delivery format is chosen on OUTPUT."""

        gdal = self._gdal()
        source_path = self._source_path(source_layer)
        dest = Path(self.project.homePath() or Path.cwd()) / ".atonce-raster"
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / f"{workflow_id[:8]}_{node_id.replace(':', '_')}_{uuid4().hex[:8]}.tif"
        translate = gdal.Translate(str(out_path), source_path, format="GTiff")
        if translate is None:
            raise RuntimeError(f"GDAL Translate failed for {source_path}")
        translate = None
        layer = self._load_raster(
            str(out_path),
            display_name or "Raster - Format Conversion",
        )
        return layer, 1

    def stage_geotiff(self, layer, staged_path: str) -> int:
        gdal = self._gdal()
        source_path = self._source_path(layer)
        Path(staged_path).parent.mkdir(parents=True, exist_ok=True)
        translate = gdal.Translate(str(staged_path), source_path, format="GTiff")
        if translate is None:
            raise RuntimeError(f"Could not stage GeoTIFF at {staged_path}")
        translate = None
        return 1

    def stage_raster_kmz(
        self, layer, staged_path: str, *, display_name: str = ""
    ) -> int:
        """Package a WGS84 PNG ground overlay into a KMZ (separate from vector KMZ)."""

        gdal = self._gdal()
        source_path = self._source_path(layer)
        staged = Path(staged_path)
        staged.parent.mkdir(parents=True, exist_ok=True)
        label = str(display_name or "").strip() or staged.stem or "Raster"
        work = staged.parent / f".atonce-raster-kmz-{uuid4().hex[:8]}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            png_path = work / "overlay.png"
            vrt_wgs = work / "overlay_wgs84.tif"
            warp = gdal.Warp(str(vrt_wgs), source_path, dstSRS="EPSG:4326")
            if warp is None:
                raise RuntimeError("Could not reproject raster to EPSG:4326 for KMZ.")
            warp = None
            translate = gdal.Translate(str(png_path), str(vrt_wgs), format="PNG")
            if translate is None:
                raise RuntimeError("Could not render PNG overlay for KMZ.")
            translate = None
            ds = gdal.Open(str(vrt_wgs))
            if ds is None:
                raise RuntimeError("Could not read WGS84 raster extents for KMZ.")
            gt = ds.GetGeoTransform()
            width = ds.RasterXSize
            height = ds.RasterYSize
            ds = None
            north = gt[3]
            south = gt[3] + height * gt[5]
            west = gt[0]
            east = gt[0] + width * gt[1]
            safe_name = xml_escape(label, entities={'"': "&quot;", "'": "&apos;"})
            kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <GroundOverlay>
    <name>{safe_name}</name>
    <Icon><href>overlay.png</href></Icon>
    <LatLonBox>
      <north>{north}</north>
      <south>{south}</south>
      <east>{east}</east>
      <west>{west}</west>
    </LatLonBox>
  </GroundOverlay>
</kml>
"""
            (work / "doc.kml").write_text(kml, encoding="utf-8")
            with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(work / "doc.kml", arcname="doc.kml")
                archive.write(png_path, arcname="overlay.png")
        finally:
            for path in sorted(work.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    path.rmdir()
        return 1
