"""Tests for pricing.py lru_cache behavior (Task #4)."""
from __future__ import annotations


from veronica_core.pricing import resolve_model_pricing


class TestPricingLruCache:
    def setup_method(self) -> None:
        """Clear cache before each test for isolation."""
        resolve_model_pricing.cache_clear()

    def test_lru_cache_present(self) -> None:
        """resolve_model_pricing must have lru_cache attached."""
        assert hasattr(resolve_model_pricing, "cache_info")
        assert hasattr(resolve_model_pricing, "cache_clear")

    def test_cached_result_identical_object(self) -> None:
        """Second call with same model returns same Pricing object (cache hit)."""
        p1 = resolve_model_pricing("gpt-4o")
        p2 = resolve_model_pricing("gpt-4o")
        assert p1 is p2

    def test_cache_miss_increments_misses(self) -> None:
        """First call to a new model must record a cache miss."""
        resolve_model_pricing("gpt-4o")
        info = resolve_model_pricing.cache_info()
        assert info.misses >= 1

    def test_cache_hit_increments_hits(self) -> None:
        """Repeated call to same model must record a cache hit."""
        resolve_model_pricing("gpt-4o")
        resolve_model_pricing("gpt-4o")
        info = resolve_model_pricing.cache_info()
        assert info.hits >= 1

    def test_different_models_cached_separately(self) -> None:
        """Different model strings must have separate cache entries."""
        p1 = resolve_model_pricing("gpt-4o")
        p2 = resolve_model_pricing("o3")
        assert p1 is not p2

    def test_cache_clear_resets_counts(self) -> None:
        """cache_clear() must reset hit/miss counters and evict entries."""
        resolve_model_pricing("gpt-4o")
        resolve_model_pricing("gpt-4o")
        resolve_model_pricing.cache_clear()
        info = resolve_model_pricing.cache_info()
        assert info.hits == 0
        assert info.misses == 0
        assert info.currsize == 0
