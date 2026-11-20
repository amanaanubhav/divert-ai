"""Redis key registry: naming, validation and Redis Cluster slot co-location."""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from redis.crc import key_slot

from divert.store import redis_store as k


def test_per_city_keys_share_one_cluster_slot() -> None:
    city_keys = [
        k.vehicles("delhi"),
        k.vehicle_route("delhi", "AMB-Hn1-1"),
        k.claims("delhi", "J123"),
        k.vehicle_claims("delhi", "AMB-Hn1-1"),
        k.active_signals("delhi"),
        k.city_tick("delhi"),
    ]
    assert len(set(city_keys)) == len(city_keys)
    assert len({key_slot(key.encode()) for key in city_keys}) == 1  # one MULTI/EXEC per city
    assert key_slot(k.vehicles("delhi").encode()) != key_slot(k.vehicles("kolkata").encode())


def test_key_shapes() -> None:
    assert k.vehicles("kolkata") == "divert:{kolkata}:vehicles"
    assert k.claims("kolkata", "J42") == "divert:{kolkata}:claims:J42"
    assert k.events_channel("kolkata") == "divert:events:kolkata"
    assert k.STREAM_COMMANDS == "divert:commands"
    assert k.STREAM_HISTORY == "divert:history"
    assert k.STREAM_HISTORY_DLQ == "divert:history:dlq"
    assert k.osrm_route_cache("driving", "77.1,28.6;77.2,28.7").startswith("divert:osrm:driving:")
    assert k.rate_limit("post", "10.0.0.1", 123).endswith(":123")
    assert "10.0.0.1" not in k.rate_limit("post", "10.0.0.1", 123)  # client ids are hashed


@pytest.mark.parametrize("bad", ["", "a b", "a:b", "{x}", "x" * 65, "del*"])
def test_invalid_ids_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        k.vehicles(bad)
    with pytest.raises(ValueError, match="invalid"):
        k.claims("delhi", bad)


async def test_per_vehicle_fields_do_not_clobber_each_other() -> None:
    """Guarantee 3 at the storage level: two writers own disjoint hash fields."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    key = k.vehicles("delhi")
    async with r.pipeline(transaction=True) as p:
        p.hset(key, "AMB-1", '{"along_m": 10}')
        p.hset(k.claims("delhi", "J1"), "AMB-1", '{"eta_s": 30}')
        p.sadd(k.vehicle_claims("delhi", "AMB-1"), "J1")
        await p.execute()
    await r.hset(key, "PCR-2", '{"along_m": 99}')
    assert await r.hgetall(key) == {"AMB-1": '{"along_m": 10}', "PCR-2": '{"along_m": 99}'}
    assert await r.smembers(k.vehicle_claims("delhi", "AMB-1")) == {"J1"}
    await r.aclose()
