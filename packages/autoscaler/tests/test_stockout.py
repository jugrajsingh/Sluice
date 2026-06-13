from sluice_autoscaler.placement import Candidate, candidate_key
from sluice_autoscaler.stockout import StockoutBoard
from sluice_core.drivers.cache_objectstore import ObjectStoreCache
from sluice_core.testing.fakes import FakeObjectStore


def _cand(zone="us-central1-a"):
    return Candidate(substrate="kubernetes", pricing="spot", provider="k8s", location=zone, gpu_type="nvidia-l4")


async def test_mark_and_view():
    board = StockoutBoard(cache=ObjectStoreCache(store=FakeObjectStore()), ttl_s=60)
    await board.mark(candidate_key(_cand()), "ZONE_RESOURCE_POOL_EXHAUSTED")
    view = await board.view([candidate_key(_cand()), candidate_key(_cand("us-central1-c"))])
    assert view == {candidate_key(_cand()): "ZONE_RESOURCE_POOL_EXHAUSTED"}


async def test_shared_across_apps_via_same_cache():
    cache = ObjectStoreCache(store=FakeObjectStore())  # the controller holds ONE cache for all apps
    board_a, board_b = StockoutBoard(cache=cache, ttl_s=60), StockoutBoard(cache=cache, ttl_s=60)
    await board_a.mark(candidate_key(_cand()), "stockout-by-app-a")
    assert (await board_b.view([candidate_key(_cand())])) != {}
