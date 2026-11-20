"""``divert`` command line: migrate | seed | fetch-data.

divert migrate            # alembic upgrade head + idempotent seed (the 'migrate' job)
divert migrate --no-seed
divert seed [--city delhi]
divert fetch-data [delhi kolkata ...] [--cache-dir DIR]   # refresh OSM snapshots
divert dlq [--replay] [--id ID ...]   # inspect / re-queue dead-lettered history events
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from divert.cities.loader import load_snapshot
from divert.config import Settings, get_settings
from divert.db.seed import SeedReport, seed_city
from divert.db.session import create_engine, create_sessionmaker, transaction
from divert.logging import configure_logging

log = logging.getLogger("divert.cli")
SEED_LOCK_KEY = 0x0D17E27  # pg advisory lock: one seeder at a time


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "divert:migrations")
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def run_migrations(database_url: str, revision: str = "head") -> None:
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    command.upgrade(alembic_config(database_url), revision)


async def seed_all(settings: Settings, cities: Sequence[str] | None = None) -> list[SeedReport]:
    engine = create_engine(settings, application_name="divert-seed")
    try:
        sm = create_sessionmaker(engine)
        reports: list[SeedReport] = []
        async with transaction(sm) as session:
            await session.execute(sa.select(sa.func.pg_advisory_xact_lock(SEED_LOCK_KEY)))
            for city_id in cities or settings.cities:
                snap = load_snapshot(city_id)
                reports.append(await seed_city(session, snap, settings.units_per_station))
            # fresh statistics so the planner picks the GiST paths right after a bulk load
            await session.execute(sa.text("ANALYZE cities, stations, units, signals"))
        return reports
    finally:
        await engine.dispose()


async def dead_letters(settings: Settings, *, replay: bool, ids: Sequence[str] = ()) -> int:
    """List the history DLQ, or move entries back onto divert:history (after a fix)."""
    from divert.store import redis_store as keys  # noqa: PLC0415
    from divert.store.streams import FIELD, to_str  # noqa: PLC0415

    redis = keys.create_redis(settings)
    try:
        entries: list[Any] = list(await redis.xrange(keys.STREAM_HISTORY_DLQ) or [])
        n = 0
        for entry_id, fields in entries:
            eid = to_str(entry_id)
            if ids and eid not in ids:
                continue
            record = json.loads(to_str(fields[FIELD]))
            n += 1
            if not replay:
                print(f"{eid} deliveries={record.get('deliveries')} reason={record.get('reason')}")
                print(f"    {record.get('raw')}")
                continue
            async with redis.pipeline(transaction=True) as p:
                p.xadd(keys.STREAM_HISTORY, {FIELD: record["raw"]})
                p.xdel(keys.STREAM_HISTORY_DLQ, eid)
                await p.execute()
            print(f"re-queued {eid}")
        print(f"{n} dead letter(s) {'re-queued' if replay else 'listed'}")
        return n
    finally:
        await redis.aclose()


def _print_reports(reports: list[SeedReport]) -> None:
    for r in reports:
        print(
            f"seeded {r.city_id}: stations={r.stations} signals={r.signals} "
            f"units={r.units_total} (+{r.units_created}) "
            f"deactivated stations={r.stations_deactivated} signals={r.signals_deactivated}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="divert", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_mig = sub.add_parser("migrate", help="alembic upgrade head, then seed")
    p_mig.add_argument("--no-seed", action="store_true")
    p_seed = sub.add_parser("seed", help="upsert reference data from the OSM snapshots")
    p_seed.add_argument("--city", action="append", dest="cities")
    p_fetch = sub.add_parser("fetch-data", help="re-fetch OSM snapshots from Overpass")
    p_fetch.add_argument("cities", nargs="*")
    p_fetch.add_argument("--cache-dir", default=None)
    p_dlq = sub.add_parser("dlq", help="list (default) or --replay the history DLQ")
    p_dlq.add_argument("--replay", action="store_true")
    p_dlq.add_argument("--id", action="append", dest="ids", default=[])
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("cli", settings.log_level, json_logs=settings.log_json)

    if args.cmd == "fetch-data":
        from divert.cities import osm  # noqa: PLC0415 - optional, network-only path

        extra = ["--cache-dir", args.cache_dir] if args.cache_dir else []
        return osm.main([*args.cities, *extra])
    if args.cmd == "dlq":
        asyncio.run(dead_letters(settings, replay=args.replay, ids=args.ids))
        return 0
    if args.cmd == "migrate":
        run_migrations(settings.sqlalchemy_url)
        log.info("migrations at head")
        if args.no_seed:
            return 0
    _print_reports(asyncio.run(seed_all(settings, getattr(args, "cities", None))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
