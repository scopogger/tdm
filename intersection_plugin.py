"""
Main plugin class. QGIS instantiates this once (via classFactory in
__init__.py) and calls initGui() on load, unload() on unload.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QAction, QStyle

from .intersection_engine import IntersectionEngine
from .layer_selector_panel import LayerSelectorPanel
from .polygon_capture_tool import PolygonCaptureTool
from .reactive_watcher import ReactiveWatcher


class IntersectionPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.action = None
        self.panel = None
        self.capture_tool = None
        self.engine = None
        self.watcher = None

    def initGui(self):
        icon = self.iface.mainWindow().style().standardIcon(QStyle.SP_DialogApplyButton)
        self.action = QAction(icon, "Selection Intersector", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self._toggle_panel)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&Selection Intersector", self.action)

        self.panel = LayerSelectorPanel(self.iface)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.panel)
        self.panel.hide()
        self.panel.visibilityChanged.connect(self.action.setChecked)

        self.engine = IntersectionEngine(self.iface)
        self.watcher = ReactiveWatcher()
        self.watcher.sourceDataChanged.connect(self._trigger_recalculation)

        self.capture_tool = PolygonCaptureTool(self.canvas)
        self.capture_tool.polygonCaptured.connect(self._on_polygon_captured)

        self.panel.drawPolygonRequested.connect(self._start_polygon_capture)
        self.panel.recalcRequested.connect(self._trigger_recalculation)
        self.panel.selectionChanged.connect(self._on_selection_changed)
        self.panel.autoUpdateToggled.connect(self._on_auto_update_toggled)
        self.panel.outputNameChanged.connect(self.engine.set_output_name)

    def unload(self):
        self.iface.removePluginMenu("&Selection Intersector", self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.watcher is not None:
            self.watcher.stop()
        if self.capture_tool is not None and self.canvas.mapTool() is self.capture_tool:
            self.canvas.unsetMapTool(self.capture_tool)
        if self.panel is not None:
            self.iface.removeDockWidget(self.panel)
            self.panel.deleteLater()

    # -- UI callbacks ------------------------------------------------------

    def _toggle_panel(self, checked):
        self.panel.setVisible(checked)

    def _start_polygon_capture(self):
        self.canvas.setMapTool(self.capture_tool)
        self.panel.set_status(
            "Click to add vertices, right-click (or Enter) to finish, Esc to cancel."
        )

    def _on_polygon_captured(self, geometry):
        self.canvas.unsetMapTool(self.capture_tool)
        self.engine.set_polygon(geometry, self.canvas.mapSettings().destinationCrs())
        self.panel.set_status("Polygon captured -- calculating...")
        self._trigger_recalculation()

    def _on_selection_changed(self, layers):
        self.engine.set_source_layers(layers)
        if self.panel.auto_update_enabled():
            self.watcher.watch(layers)
            self._trigger_recalculation()
        else:
            self.watcher.stop()

    def _on_auto_update_toggled(self, enabled):
        if enabled:
            self.watcher.watch(self.panel.selected_layers())
            self._trigger_recalculation()
        else:
            self.watcher.stop()

    def _trigger_recalculation(self):
        self.panel.set_status("Calculating...")
        self.engine.recalculate(on_done=self._on_recalculated)

    def _on_recalculated(self, feature_count, error):
        if error:
            self.panel.set_status(f"Error: {error}")
        else:
            self.panel.set_status(f"Done -- {feature_count} intersecting feature(s).")
