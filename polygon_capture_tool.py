"""
Interactive map tool for capturing an arbitrary polygon on the canvas.

Left-click adds a vertex, right-click or Enter/Return finishes the
polygon (emitting polygonCaptured), Escape cancels the current
polygon. A rubber band shows the polygon-so-far plus a preview edge
that follows the cursor.

Built on plain QgsMapTool (not QgsMapToolEmitPoint, which is meant
for single press/release captures like a rectangle) since we need to
accumulate an arbitrary number of vertices ourselves.
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QCursor
from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand


class PolygonCaptureTool(QgsMapTool):
    polygonCaptured = pyqtSignal(QgsGeometry)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.points = []
        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(QColor(255, 140, 0, 90))
        self.rubber_band.setWidth(2)
        self.setCursor(QCursor(Qt.CrossCursor))

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self.finish()
            return
        if event.button() == Qt.LeftButton:
            point = self.toMapCoordinates(event.pos())
            self.points.append(QgsPointXY(point))
            self._redraw(preview_point=None)

    def canvasMoveEvent(self, event):
        if not self.points:
            return
        preview_point = self.toMapCoordinates(event.pos())
        self._redraw(preview_point=preview_point)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reset()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish()

    def _redraw(self, preview_point):
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        all_points = list(self.points)
        if preview_point is not None:
            all_points.append(preview_point)
        last_index = len(all_points) - 1
        for i, p in enumerate(all_points):
            # doUpdate=True only on the last point avoids a repaint per vertex
            self.rubber_band.addPoint(p, i == last_index)

    def finish(self):
        if len(self.points) >= 3:
            ring = list(self.points) + [self.points[0]]
            geometry = QgsGeometry.fromPolygonXY([ring])
            self.polygonCaptured.emit(geometry)
        self.reset()

    def reset(self):
        self.points = []
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)

    def deactivate(self):
        self.reset()
        super().deactivate()
