"""
QGIS plugin loader entry point.

QGIS looks for a top-level function called classFactory() in this
file when the plugin is loaded. It must return an object with
initGui() and unload() methods.
"""


def classFactory(iface):
    from .plugin import CopyCorrectPlugin
    return CopyCorrectPlugin(iface)
