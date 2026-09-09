"""
Select Across Layers: a small utility tool.

QGIS's own selection tools always act on a single "active" layer.
This tool instead reads whichever layers are highlighted in the
Layers panel -- the normal Ctrl/Shift-click multi-select QGIS already
supports there -- and applies one rectangle drag to every one of them
at once.

Modifiers while dragging match QGIS's own selection tools:
  - plain drag    -> replace each layer's selection
  - Shift + drag  -> add to each layer's existing selection
  - Ctrl + drag   -> remove from each layer's existing selection

If nothing is highlighted in the Layers panel when a rectangle is
drawn, the tool falls back to the single active layer, so it never
just silently does nothing (matching the native tool's behaviour).

This is a general-purpose selection utility, unrelated to any one
workflow, which is why it's a separate plugin rather than folded into
Copy & Correct.
"""

from qgis.PyQt.QtCore import QPoint, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QAction

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

try:
    _SELECT_BEHAVIOR = Qgis.SelectBehavior  # QGIS versions with the unified Qgis enums
except AttributeError:
    _SELECT_BEHAVIOR = QgsVectorLayer.SelectBehaviour  # older QGIS

# A near-zero drag is still treated as a click, using a small pixel
# tolerance around the point -- matches the native rectangle tool.
CLICK_TOLERANCE_PX = 2
CLICK_PAD_PX = 3


class MultiLayerSelectTool(QgsMapTool):
    """Rectangle-select tool that applies to every layer highlighted in
    the Layers panel, instead of just the single active layer."""

    def __init__(self, canvas, iface):
        super().__init__(canvas)
        self.iface = iface
        self.setCursor(Qt.CrossCursor)
        self._start_px = None
        self._transform_cache = {}
        self._rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._rubber_band.setFillColor(QColor(0, 120, 250, 60))
        self._rubber_band.setStrokeColor(QColor(0, 120, 250, 220))
        self._rubber_band.setWidth(1)

    def canvasPressEvent(self, event):
        self._start_px = event.pos()
        self._update_band(event.pos())

    def canvasMoveEvent(self, event):
        if self._start_px is None:
            return
        self._update_band(event.pos())

    def canvasReleaseEvent(self, event):
        if self._start_px is None:
            return
        start_px = self._start_px
        self._start_px = None
        self._rubber_band.reset(QgsWkbTypes.PolygonGeometry)

        end_px = event.pos()
        if (end_px - start_px).manhattanLength() <= CLICK_TOLERANCE_PX:
            p1 = QPoint(end_px.x() - CLICK_PAD_PX, end_px.y() - CLICK_PAD_PX)
            p2 = QPoint(end_px.x() + CLICK_PAD_PX, end_px.y() + CLICK_PAD_PX)
        else:
            p1, p2 = start_px, end_px

        rect = QgsRectangle(self.toMapCoordinates(p1), self.toMapCoordinates(p2))
        rect.normalize()
        self._select_in_layers(rect, event.modifiers())

    def deactivate(self):
        self._start_px = None
        self._rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        super().deactivate()

    def _update_band(self, current_px):
        rect = QgsRectangle(self.toMapCoordinates(self._start_px), self.toMapCoordinates(current_px))
        rect.normalize()
        self._rubber_band.setToGeometry(QgsGeometry.fromRect(rect), None)

    def _rect_in_layer_crs(self, rect, canvas_crs, layer):
        """selectByRect() expects the rectangle in the LAYER's own CRS,
        not the canvas's -- if a layer is being reprojected on the fly
        (a different CRS from the canvas), using the raw canvas-space
        rect selects the wrong features entirely, which is what was
        producing the offset."""
        if layer.crs() == canvas_crs:
            return rect
        key = layer.crs().authid() or layer.crs().toWkt()
        transform = self._transform_cache.get(key)
        if transform is None:
            transform = QgsCoordinateTransform(canvas_crs, layer.crs(), QgsProject.instance())
            self._transform_cache[key] = transform
        return transform.transformBoundingBox(rect)

    def _select_in_layers(self, rect, modifiers):
        layers = [
            layer for layer in self.iface.layerTreeView().selectedLayers()
            if isinstance(layer, QgsVectorLayer)
        ]
        if not layers:
            active = self.iface.activeLayer()
            if isinstance(active, QgsVectorLayer):
                layers = [active]
        if not layers:
            self.iface.messageBar().pushMessage(
                'Выбор по нескольким слоям',
                'Выделите один или несколько слоёв в панели «Слои» '
                '(или сделайте активным векторный слой).',
                level=Qgis.Warning,
                duration=4,
            )
            return

        if modifiers & Qt.ShiftModifier:
            behavior = _SELECT_BEHAVIOR.AddToSelection
        elif modifiers & Qt.ControlModifier:
            behavior = _SELECT_BEHAVIOR.RemoveFromSelection
        else:
            behavior = _SELECT_BEHAVIOR.SetSelection

        canvas_crs = self.canvas().mapSettings().destinationCrs()
        for layer in layers:
            layer.selectByRect(self._rect_in_layer_crs(rect, canvas_crs, layer), behavior)

        total_selected = sum(layer.selectedFeatureCount() for layer in layers)
        self.iface.messageBar().pushMessage(
            'Выбор по нескольким слоям',
            'Выделено объектов: {0} (слоёв: {1}).'.format(total_selected, len(layers)),
            level=Qgis.Info,
            duration=3,
        )


class MultiLayerSelectPlugin:
    """Plugin entry point: registers a toggleable map tool."""

    MENU_NAME = '&Выбор по нескольким слоям'

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.tool = None

    def initGui(self):
        icon = QgsApplication.getThemeIcon('mActionSelectRectangle.svg')
        self.action = QAction(icon, 'Выбор объектов на нескольких слоях', self.iface.mainWindow())
        self.action.setCheckable(True)
        self.iface.addPluginToMenu(self.MENU_NAME, self.action)
        self.iface.addToolBarIcon(self.action)

        self.tool = MultiLayerSelectTool(self.iface.mapCanvas(), self.iface)
        self.action.triggered.connect(self._activate)
        self.iface.mapCanvas().mapToolSet.connect(self._on_map_tool_set)

    def unload(self):
        self.iface.mapCanvas().mapToolSet.disconnect(self._on_map_tool_set)
        self.iface.removePluginMenu(self.MENU_NAME, self.action)
        self.iface.removeToolBarIcon(self.action)
        self.tool = None

    def _activate(self):
        # Qt auto-toggles a checkable action's own checked state on every
        # click, so clicking the button while it's already active would
        # otherwise pop it back out without the tool actually
        # deactivating. Force it back to checked every time -- it only
        # goes unchecked in `_on_map_tool_set`, when a different tool
        # genuinely takes over.
        self.iface.mapCanvas().setMapTool(self.tool)
        self.action.setChecked(True)

    def _on_map_tool_set(self, new_tool, old_tool):
        self.action.setChecked(new_tool is self.tool)
