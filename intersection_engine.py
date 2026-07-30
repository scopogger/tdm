"""
Core computation for the plugin.

For each selected source layer: the selection polygon is reprojected
into that layer's CRS, used as a QgsFeatureRequest.setFilterRect()
bounding-box prefilter (pushed down to the data provider -- for a
PostGIS layer this means the database's own spatial index does the
heavy lifting), then each candidate feature is precisely tested with
QgsGeometry.intersects()/.intersection().

Results from every selected layer are merged, bucketed by geometry
type (point/line/polygon -- a QGIS vector layer can only hold one),
and written into up to three output layers grouped under a
"Intersection results" layer group. Each output feature carries
source_layer and source_fid so you can trace it back to its origin.

This runs synchronously on the plugin/main thread. That's a
deliberate simplicity/correctness choice -- see "Scaling this up" in
README.md before reaching for QgsTask.
"""

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

_TYPE_INFO = {
    QgsWkbTypes.PointGeometry: ("MultiPoint", "points"),
    QgsWkbTypes.LineGeometry: ("MultiLineString", "lines"),
    QgsWkbTypes.PolygonGeometry: ("MultiPolygon", "polygons"),
}


class IntersectionEngine:
    def __init__(self, iface):
        self.iface = iface
        self.polygon_geom = None
        self.polygon_crs = None
        self.source_layers = []
        self.output_name = "intersection_result"
        self.output_group_name = "Intersection results"
        self._output_layers = {}  # geometry type -> QgsVectorLayer

    def set_polygon(self, geometry, crs):
        self.polygon_geom = geometry
        self.polygon_crs = crs

    def set_source_layers(self, layers):
        self.source_layers = [l for l in layers if isinstance(l, QgsVectorLayer)]

    def set_output_name(self, name):
        self.output_name = name or "intersection_result"
        for geom_type, layer in self._output_layers.items():
            suffix = _TYPE_INFO[geom_type][1]
            layer.setName(f"{self.output_name}_{suffix}")

    def recalculate(self, on_done=None):
        if self.polygon_geom is None or not self.source_layers:
            if on_done:
                on_done(0, None)
            return
        try:
            total = self._do_recalculate()
        except Exception as exc:  # surfaced to the status label, not raised
            if on_done:
                on_done(0, str(exc))
            return
        if on_done:
            on_done(total, None)

    # -- internals --------------------------------------------------------

    def _do_recalculate(self):
        project = QgsProject.instance()
        out_crs = project.crs()

        by_type = {}
        for layer in self.source_layers:
            for geom, source_fid in self._intersect_one_layer(layer, project):
                geom_type = QgsWkbTypes.geometryType(geom.wkbType())
                if geom_type not in _TYPE_INFO:
                    continue
                by_type.setdefault(geom_type, []).append((geom, layer.name(), source_fid))

        total = 0
        for geom_type in _TYPE_INFO:
            rows = by_type.get(geom_type, [])
            if rows:
                self._write_layer(geom_type, rows, out_crs, project)
                total += len(rows)
            elif geom_type in self._output_layers:
                self._clear_layer(self._output_layers[geom_type])
        return total

    def _intersect_one_layer(self, layer, project):
        layer_crs = layer.crs()

        poly_geom = QgsGeometry(self.polygon_geom)
        if layer_crs != self.polygon_crs:
            transform = QgsCoordinateTransform(self.polygon_crs, layer_crs, project)
            poly_geom.transform(transform)

        # Bounding-box prefilter pushed down to the provider (uses the
        # table's spatial index for a PostGIS layer) before the exact
        # geometric test below.
        request = QgsFeatureRequest().setFilterRect(poly_geom.boundingBox())

        out_crs = project.crs()
        to_out_transform = None
        if layer_crs != out_crs:
            to_out_transform = QgsCoordinateTransform(layer_crs, out_crs, project)

        rows = []
        for feat in layer.getFeatures(request):
            geom = feat.geometry()
            if geom is None or geom.isEmpty() or not geom.intersects(poly_geom):
                continue
            clipped = geom.intersection(poly_geom)
            if clipped is None or clipped.isEmpty():
                continue
            clipped = QgsGeometry(clipped)
            if to_out_transform is not None:
                clipped.transform(to_out_transform)
            rows.append((clipped, feat.id()))
        return rows

    def _get_or_create_layer(self, geom_type, out_crs, project):
        if geom_type in self._output_layers:
            return self._output_layers[geom_type]

        wkb_spec, suffix = _TYPE_INFO[geom_type]
        layer = QgsVectorLayer(
            f"{wkb_spec}?crs={out_crs.authid()}", f"{self.output_name}_{suffix}", "memory"
        )
        layer.dataProvider().addAttributes([
            QgsField("source_layer", QVariant.String),
            QgsField("source_fid", QVariant.LongLong),
        ])
        layer.updateFields()

        root = project.layerTreeRoot()
        group = root.findGroup(self.output_group_name)
        if group is None:
            group = root.insertGroup(0, self.output_group_name)
        project.addMapLayer(layer, False)
        group.addLayer(layer)

        self._output_layers[geom_type] = layer
        return layer

    def _write_layer(self, geom_type, rows, out_crs, project):
        layer = self._get_or_create_layer(geom_type, out_crs, project)
        self._clear_layer(layer)

        provider = layer.dataProvider()
        features = []
        for geom, source_name, source_fid in rows:
            f = QgsFeature(layer.fields())
            f.setGeometry(geom)
            f.setAttribute("source_layer", source_name)
            f.setAttribute("source_fid", source_fid)
            features.append(f)
        provider.addFeatures(features)
        layer.updateExtents()
        layer.triggerRepaint()

    @staticmethod
    def _clear_layer(layer):
        provider = layer.dataProvider()
        existing_ids = [f.id() for f in layer.getFeatures()]
        if existing_ids:
            provider.deleteFeatures(existing_ids)
