"""
Dock widget: lets the user pick which layers/groups feed the
intersection, draw the polygon, toggle auto-update, and see status.

The layer tree is a plain QTreeWidget mirroring the project's real
layer tree (QgsProject.instance().layerTreeRoot()), filtered to
vector layers only, since intersection is a vector operation. Group
items use Qt's built-in "auto tristate" behaviour: checking a group
checks all its layers, and a group's own checkbox reflects whether
none/some/all of its children are checked -- so "select a layers
group" and "select individual layers" both fall out of the same
tree for free.
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer, QgsMapLayer, QgsProject


class LayerSelectorPanel(QDockWidget):
    drawPolygonRequested = pyqtSignal()
    recalcRequested = pyqtSignal()
    selectionChanged = pyqtSignal(list)
    autoUpdateToggled = pyqtSignal(bool)
    outputNameChanged = pyqtSignal(str)

    def __init__(self, iface, parent=None):
        super().__init__("Selection Intersector", parent)
        self.iface = iface
        self.setObjectName("SelectionIntersectorPanel")
        self._suppress_signal = False

        container = QWidget(self)
        layout = QVBoxLayout(container)

        layout.addWidget(QLabel("Source layers / groups:"))
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.tree)

        refresh_btn = QPushButton("Refresh layer list")
        refresh_btn.clicked.connect(self.populate_tree)
        layout.addWidget(refresh_btn)

        draw_btn = QPushButton("Draw selection polygon")
        draw_btn.clicked.connect(self.drawPolygonRequested.emit)
        layout.addWidget(draw_btn)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output name:"))
        self.output_name_edit = QLineEdit("intersection_result")
        self.output_name_edit.editingFinished.connect(
            lambda: self.outputNameChanged.emit(self.output_name_edit.text())
        )
        out_row.addWidget(self.output_name_edit)
        layout.addLayout(out_row)

        self.auto_update_check = QCheckBox("Auto-update on changes")
        self.auto_update_check.setChecked(True)
        self.auto_update_check.toggled.connect(self.autoUpdateToggled.emit)
        layout.addWidget(self.auto_update_check)

        recalc_btn = QPushButton("Recalculate now")
        recalc_btn.clicked.connect(self.recalcRequested.emit)
        layout.addWidget(recalc_btn)

        self.status_label = QLabel("Ready")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addStretch()
        container.setLayout(layout)
        self.setWidget(container)

        self.populate_tree()
        QgsProject.instance().layersAdded.connect(self.populate_tree)
        QgsProject.instance().layersRemoved.connect(self.populate_tree)

    # -- tree population -----------------------------------------------

    def populate_tree(self):
        self._suppress_signal = True
        checked_ids = set(self._checked_layer_ids())
        self.tree.clear()
        root = QgsProject.instance().layerTreeRoot()
        self._add_group_items(root, self.tree.invisibleRootItem(), checked_ids)
        self._suppress_signal = False

    def _add_group_items(self, tree_group, parent_item, checked_ids):
        for child in tree_group.children():
            if isinstance(child, QgsLayerTreeGroup):
                group_item = QTreeWidgetItem(parent_item, [child.name()])
                group_item.setFlags(
                    group_item.flags() | Qt.ItemIsAutoTristate | Qt.ItemIsUserCheckable
                )
                group_item.setCheckState(0, Qt.Unchecked)
                self._add_group_items(child, group_item, checked_ids)
            elif isinstance(child, QgsLayerTreeLayer):
                layer = child.layer()
                if layer is None or layer.type() != QgsMapLayer.VectorLayer:
                    continue  # intersection only makes sense for vector layers
                layer_item = QTreeWidgetItem(parent_item, [layer.name()])
                layer_item.setFlags(layer_item.flags() | Qt.ItemIsUserCheckable)
                state = Qt.Checked if layer.id() in checked_ids else Qt.Unchecked
                layer_item.setCheckState(0, state)
                layer_item.setData(0, Qt.UserRole, layer.id())

    def _checked_layer_ids(self):
        ids = []

        def walk(item):
            for i in range(item.childCount()):
                child = item.child(i)
                layer_id = child.data(0, Qt.UserRole)
                if layer_id and child.checkState(0) == Qt.Checked:
                    ids.append(layer_id)
                walk(child)

        walk(self.tree.invisibleRootItem())
        return ids

    def _on_item_changed(self, item, column):
        if self._suppress_signal:
            return
        self.selectionChanged.emit(self.selected_layers())

    # -- public accessors used by the plugin controller ------------------

    def selected_layers(self):
        project = QgsProject.instance()
        layers = []
        for layer_id in self._checked_layer_ids():
            layer = project.mapLayer(layer_id)
            if layer is not None:
                layers.append(layer)
        return layers

    def auto_update_enabled(self):
        return self.auto_update_check.isChecked()

    def set_status(self, text):
        self.status_label.setText(text)
