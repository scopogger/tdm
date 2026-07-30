"""
Watches the selected source layers (and the project's layer tree) for
anything that should trigger a recalculation, and emits a single
debounced sourceDataChanged signal.

Three layers of reactivity, all optional/independent:

1. In-session edits: connected to each layer's *committed* signals
   (committedFeaturesAdded/Removed/committedGeometriesChanges), which
   fire once an edit session is actually saved -- not on every
   in-progress edit. This is the safer choice: it reacts to real,
   final changes rather than a half-finished edit buffer. If you'd
   rather react the instant a feature moves (before the user saves),
   connect to featureAdded/featureDeleted/geometryChanged instead
   (see QGIS_SIGNALS.md).

2. Project/group membership: QgsProject.layersRemoved and the layer
   tree root's addedChildren/removedChildren, so adding or removing a
   layer from a watched group also triggers a refresh.

3. Cross-session PostGIS edits: for layers backed by the "postgres"
   provider, we call setListening(True) and connect to the
   provider's notify signal. This reuses a real QGIS core feature
   (the same one behind the "Refresh layer on notification" checkbox
   in a PostGIS layer's Rendering properties) -- see README.md for
   the one-time SQL trigger this depends on. Without that trigger,
   edits made outside this QGIS session (by another user/process
   writing straight to PostgreSQL) will only be picked up on a
   manual "Recalculate now" or next auto-update from another cause.
"""

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.core import QgsProject


class ReactiveWatcher(QObject):
    sourceDataChanged = pyqtSignal()

    DEBOUNCE_MS = 400

    def __init__(self, parent=None):
        super().__init__(parent)
        self._connections = []  # list of (signal, slot) so we can cleanly disconnect
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.sourceDataChanged.emit)

    def watch(self, layers):
        self.stop()

        for layer in layers:
            self._connect(layer.committedFeaturesAdded, self._schedule)
            self._connect(layer.committedFeaturesRemoved, self._schedule)
            self._connect(layer.committedGeometriesChanges, self._schedule)

            provider = layer.dataProvider()
            if provider is not None and provider.name() == "postgres":
                # Starts (or reuses) a background LISTEN connection in the
                # postgres provider; see class docstring point 3.
                provider.setListening(True)
                self._connect(provider.notify, self._schedule)

        root = QgsProject.instance().layerTreeRoot()
        self._connect(root.addedChildren, self._schedule)
        self._connect(root.removedChildren, self._schedule)
        self._connect(QgsProject.instance().layersRemoved, self._schedule)

    def stop(self):
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass  # already disconnected, or the underlying object is gone
        self._connections = []
        self._timer.stop()

    def _connect(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def _schedule(self, *_args, **_kwargs):
        # Restarts on every call, so a burst of edits collapses into
        # one recalculation shortly after the burst ends.
        self._timer.start(self.DEBOUNCE_MS)
