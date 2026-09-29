"""Analysis layer for the data system.

A small, explicit three-layer split (inspired by how investment platforms are
structured):

* :mod:`datasources` — data-source layer: reads bars/macro/sector/event data
  and exposes it in plain dicts. No analysis logic here.
* :mod:`analytics` — analysis-service layer: turns raw data into statistics,
  correlations, sector rotation and backtests. Pure computation.
* :mod:`narrative` — narrative layer: turns analytics output into short,
  research-note style "要点解读" text for each view.

The web app (``apps.data_server``) is the API/routing + presentation layer on
top of these. Keeping the layers separate makes it easy to swap the CSV/macro
sources for real data feeds later without touching the analytics or the UI.
"""
