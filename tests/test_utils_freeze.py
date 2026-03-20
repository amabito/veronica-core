"""Tests for veronica_core._utils.freeze_mapping()."""

from __future__ import annotations

import types

import pytest

from veronica_core._utils import freeze_mapping


# ---------------------------------------------------------------------------
# T8: freeze_mapping()
# ---------------------------------------------------------------------------


class TestFreezeMappingBasic:
    """Basic behavior of freeze_mapping()."""

    def test_replaces_dict_with_mapping_proxy(self) -> None:
        """freeze_mapping() converts a plain dict to a MappingProxyType."""
        from dataclasses import dataclass, field

        @dataclass(frozen=True)
        class _Holder:
            metadata: dict = field(default_factory=dict)

            def __post_init__(self) -> None:
                freeze_mapping(self, "metadata")

        obj = _Holder(metadata={"a": 1})
        assert isinstance(obj.metadata, types.MappingProxyType)

    def test_proxy_preserves_contents(self) -> None:
        """The frozen MappingProxyType must contain the same key-value pairs."""
        from dataclasses import dataclass, field

        @dataclass(frozen=True)
        class _Holder:
            data: dict = field(default_factory=dict)

            def __post_init__(self) -> None:
                freeze_mapping(self, "data")

        obj = _Holder(data={"x": 42, "y": "hello"})
        assert obj.data["x"] == 42
        assert obj.data["y"] == "hello"

    def test_mutation_raises_type_error(self) -> None:
        """Mutating the frozen proxy must raise TypeError."""
        from dataclasses import dataclass, field

        @dataclass(frozen=True)
        class _Holder:
            metadata: dict = field(default_factory=dict)

            def __post_init__(self) -> None:
                freeze_mapping(self, "metadata")

        obj = _Holder(metadata={"k": "v"})
        with pytest.raises(TypeError):
            obj.metadata["injected"] = "evil"  # type: ignore[index]

    def test_deleting_key_raises_type_error(self) -> None:
        """Deleting a key from the frozen proxy must raise TypeError."""
        from dataclasses import dataclass, field

        @dataclass(frozen=True)
        class _Holder:
            data: dict = field(default_factory=dict)

            def __post_init__(self) -> None:
                freeze_mapping(self, "data")

        obj = _Holder(data={"a": 1})
        with pytest.raises(TypeError):
            del obj.data["a"]  # type: ignore[attr-defined]

    def test_empty_dict_becomes_empty_proxy(self) -> None:
        """freeze_mapping() on an empty dict yields an empty MappingProxyType."""
        from dataclasses import dataclass, field

        @dataclass(frozen=True)
        class _Holder:
            data: dict = field(default_factory=dict)

            def __post_init__(self) -> None:
                freeze_mapping(self, "data")

        obj = _Holder(data={})
        assert isinstance(obj.data, types.MappingProxyType)
        assert len(obj.data) == 0


class TestFreezeMappingEdgeCases:
    """Edge-case tests for freeze_mapping()."""

    def test_nested_dict_outer_frozen_inner_still_mutable(self) -> None:
        """freeze_mapping() freezes only the top-level mapping.
        Nested dicts remain mutable (MappingProxyType is shallow)."""
        from dataclasses import dataclass, field

        @dataclass(frozen=True)
        class _Holder:
            metadata: dict = field(default_factory=dict)

            def __post_init__(self) -> None:
                freeze_mapping(self, "metadata")

        inner = {"nested_key": "nested_value"}
        obj = _Holder(metadata={"outer": inner})
        # Outer mapping is frozen
        with pytest.raises(TypeError):
            obj.metadata["new_key"] = "x"  # type: ignore[index]
        # But inner dict is still mutable (shallow freeze)
        obj.metadata["outer"]["nested_key"] = "mutated"
        assert obj.metadata["outer"]["nested_key"] == "mutated"

    def test_independent_instances_do_not_share_proxy(self) -> None:
        """Two instances with the same initial dict content get separate proxies."""
        from dataclasses import dataclass, field

        @dataclass(frozen=True)
        class _Holder:
            data: dict = field(default_factory=dict)

            def __post_init__(self) -> None:
                freeze_mapping(self, "data")

        a = _Holder(data={"k": 1})
        b = _Holder(data={"k": 1})
        assert a.data is not b.data
