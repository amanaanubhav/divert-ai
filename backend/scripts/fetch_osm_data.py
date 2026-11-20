"""Fetch the committed OpenStreetMap snapshots (thin wrapper around divert.cities.osm).

Usage (from backend/):
    uv run python scripts/fetch_osm_data.py              # all cities
    uv run python scripts/fetch_osm_data.py delhi kolkata
    uv run divert fetch-data                             # same thing via the CLI

See divert/cities/osm.py and docs/data-sources.md for what is fetched and how.
"""

from divert.cities.osm import main

if __name__ == "__main__":
    raise SystemExit(main())
