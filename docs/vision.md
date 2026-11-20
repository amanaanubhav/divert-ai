# DIVERT - vision

**DIVERT (Dynamic Intelligent Vehicle Emergency Routing & Traffic)**: because every second counts.

> This page preserves the original project idea (formerly `infodoc.txt`). Where the
> platform as built differs, an *As built* note says so and links to the authoritative doc.

## 1. Problem and vision

Emergency vehicles lose critical minutes in urban traffic. DIVERT bridges emergency-vehicle
tracking and urban traffic infrastructure to create a **green corridor that moves with the
vehicle**: signals ahead of an ambulance, fire engine or police car are pre-empted just in
time and released right after it passes.

## 2. System concept

A hub-and-spoke model:

- **Vehicle client** - reports GPS position and emergency status (simulated today).
- **DIVERT core** - processes telemetry, computes routes and drives the signal network.
- **Traffic signal controllers** - software interfacing with physical signal heads and
  lane-indicator boards.
- **Dashboard** - the command-centre UI.

*As built:* the core is split into a stateless API, a single-leader simulation worker and a
history persister around PostgreSQL/PostGIS (system of record) and Redis (hot state +
messaging). See [architecture.md](architecture.md) and [data-model.md](data-model.md).

## 3. Core logic

**A. Dynamic geofencing.** Instead of a static route lock, a rolling ~600 m *influence zone*
travels with the vehicle; infrastructure inside it enters emergency-priority mode.
*As built:* a corridor signal is claimed when the vehicle's ETA to it is <= 45 s **or** it is
<= 600 m ahead along the route, and released 20 m after the vehicle passes (and always on
arrival, cancel or error).

**B. Lane diversion.** Signals are assumed to carry priority-lane LED boards that tell other
traffic which lane to clear. The original idea reserved the leftmost lane for the
emergency vehicle. *As built:* India drives on the left, so the advisory follows the
vehicle's manoeuvre at that junction: a left turn keeps it in the left lane (`L`); straight
on, right turns and U-turns use the right lane (`R`), and other traffic is told to keep
clear of it.

**C. ETA prediction.** Static ETAs fail under emergency conditions. The idea was an ML model
considering route vs. straight-line distance, congestion, and vehicle acceleration profiles
(ambulance vs. heavy fire engine). *As built:* a transparent physics-based model (OSRM
segment speeds x city hourly congestion x emergency advantage, per-vehicle acceleration and
braking, junction slow-downs) behind an `EtaModel` protocol, so a trained model can replace
it once real incident history exists. The prototype's RandomForest trained on synthetic
data was removed.

## 4. Development stages

1. Real-time sync between backend (FastAPI) and frontend (React + Leaflet).
2. OSRM street routing instead of straight lines.
3. Branding, UI refinement, ML integration.
4. Scale to thousands of concurrent vehicles and multiple cities - *in progress:* Delhi,
   Kolkata and Bengaluru on real OpenStreetMap data, horizontally scalable services.

## 5. Future scope

- **IoT integration** - direct signal-controller control over MQTT for low-latency switching.
- **Civilian navigation SDK** - warn drivers in navigation apps: "Emergency vehicle
  approaching - keep clear".
- **Adaptive rerouting** - push a reroute to the vehicle when a blockage is detected mid-route.
- **Predictive deployment** - use incident history (now stored in PostGIS) to pre-position
  units in high-risk zones.

## 6. Hardware (conceptual)

- Traffic signals with multi-segment LED lane displays.
- Vehicle transponders (DSRC / C-V2X) for sub-metre positioning.
