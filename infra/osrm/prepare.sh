#!/usr/bin/env bash
# Build a self-hosted OSRM dataset covering DIVERT's cities (Delhi, Kolkata, Bengaluru).
#
#   Geofabrik India extract -> osmium clip per city bbox (+margin) -> merge -> OSRM MLD
#
# Requirements: curl, osmium-tool (apt install osmium-tool | brew install osmium-tool),
# docker. Disk: ~2 GB download + ~1 GB work files. Time: ~10-20 min on a laptop.
# Output: infra/osrm/data/divert-cities.osrm* (gitignored), served by the compose
# profile 'osrm' (docker compose --profile osrm up -d osrm) on :5000.
# Then set DIVERT_OSRM_URL=http://osrm:5000 (inside compose) or http://localhost:5000.
#
# Documented, not run in CI. Re-run monthly (or on demand) to pick up OSM edits.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${HERE}/data"
OSRM_IMAGE="${OSRM_IMAGE:-ghcr.io/project-osrm/osrm-backend:v5.27.1}"
SOURCE_URL="${SOURCE_URL:-https://download.geofabrik.de/asia/india-latest.osm.pbf}"
MARGIN="${MARGIN:-0.05}"   # degrees added around each city bbox (routes may leave the bbox)
NAME="divert-cities"

# Same bboxes as backend/src/divert/cities/osm.py  (west,south,east,north for osmium)
declare -A BBOX=(
  [delhi]="76.84,28.40,77.35,28.88"
  [kolkata]="88.25,22.45,88.50,22.65"
  [bengaluru]="77.46,12.83,77.78,13.14"
)

command -v osmium >/dev/null || { echo "osmium-tool is required" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
mkdir -p "${DATA}"
cd "${DATA}"

if [[ ! -s india-latest.osm.pbf ]]; then
  echo ">> downloading ${SOURCE_URL}"
  curl -fL --retry 5 -o india-latest.osm.pbf.part "${SOURCE_URL}"
  curl -fsL "${SOURCE_URL}.md5" -o india-latest.osm.pbf.md5 || true
  mv india-latest.osm.pbf.part india-latest.osm.pbf
  if [[ -s india-latest.osm.pbf.md5 ]]; then
    echo "$(cut -d' ' -f1 india-latest.osm.pbf.md5)  india-latest.osm.pbf" | md5sum -c -
  fi
fi

parts=()
for city in "${!BBOX[@]}"; do
  IFS=, read -r w s e n <<<"${BBOX[$city]}"
  bb="$(awk -v w="$w" -v s="$s" -v e="$e" -v n="$n" -v m="$MARGIN" \
        'BEGIN{printf "%.4f,%.4f,%.4f,%.4f", w-m, s-m, e+m, n+m}')"
  echo ">> clipping ${city} (${bb})"
  osmium extract --overwrite --strategy complete_ways -b "${bb}" india-latest.osm.pbf -o "${city}.osm.pbf"
  parts+=("${city}.osm.pbf")
done

echo ">> merging ${parts[*]}"
osmium merge --overwrite "${parts[@]}" -o "${NAME}.osm.pbf"

run() { docker run --rm -t -v "${DATA}:/data" "${OSRM_IMAGE}" "$@"; }
echo ">> osrm-extract (car profile)"
run osrm-extract -p /opt/car.lua "/data/${NAME}.osm.pbf"
echo ">> osrm-partition"
run osrm-partition "/data/${NAME}.osrm"
echo ">> osrm-customize"
run osrm-customize "/data/${NAME}.osrm"
echo ">> done: ${DATA}/${NAME}.osrm (start with: docker compose --profile osrm up -d osrm)"
