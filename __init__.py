"""
Geometry Repair Tool
---------------------
QGIS entry point. QGIS calls classFactory(iface) to instantiate the plugin
when it loads the plugin listed in metadata.txt.
"""


def classFactory(iface):
    from .geometry_repair import GeometryRepairPlugin
    return GeometryRepairPlugin(iface)
