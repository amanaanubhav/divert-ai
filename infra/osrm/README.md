# Self-hosted OSRM

DIVERT routes with OSRM (`/route/v1/driving`, `overview=full`, `geometries=geojson`,
`steps=true`, `annotations=distance,duration,speed`). By default it calls the public demo
server `https://router.project-osrm.org`, which is for **light, fair use only** (no SLA,
rate-limited). Production must self-host:

```bash
infra/osrm/prepare.sh                          # Geofabrik India -> 3 city clips -> OSRM MLD
docker compose --profile osrm up -d osrm       # serves :5000
# api/worker: DIVERT_OSRM_URL=http://osrm:5000 (compose network) or http://localhost:5000
```

What `prepare.sh` does:

1. downloads `asia/india-latest.osm.pbf` from Geofabrik (verifies the published MD5);
2. clips each city bbox (+0.05 degree margin, `complete_ways`) with `osmium extract`, then
   merges the three clips (one OSRM dataset serves all cities and keeps memory small,
   ~1-2 GB RAM for `osrm-routed`);
3. runs `osrm-extract -p car.lua`, `osrm-partition`, `osrm-customize` (MLD algorithm,
   so a traffic-weighted re-customize can later run without a full rebuild).

Output lives in `infra/osrm/data/` (gitignored). Refresh monthly. The car profile has no
emergency-vehicle rules; DIVERT applies its own speed model on top of OSRM's segment
speeds (see docs/simulation-model.md).

Data: (c) OpenStreetMap contributors, ODbL. OSRM: BSD-2-Clause.
