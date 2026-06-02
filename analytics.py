"""Step 9 — Analytics layer.

Batch jobs producing GeoJSON for Leaflet map overlays:
  - Route density heatmap (trips per geographic hex-bin or grid cell)
  - Geographic gap analysis (areas with low stop density)

Uses GeoPandas + Shapely.  Output files written to static/ for FastAPI to serve.
"""

# TODO: implement compute_density(conn) -> GeoJSON FeatureCollection
# TODO: implement compute_gaps(conn) -> GeoJSON FeatureCollection
