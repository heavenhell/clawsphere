"""Adapters for real northbound API integrations (eDME, FusionCompute)."""

from backend.adapters.common import coalesce, extract_items, ratio, resource_id, total
from backend.adapters.edme import EDMERestAdapter
from backend.adapters.fusioncompute import FusionComputeRestAdapter

__all__ = [
    "EDMERestAdapter",
    "FusionComputeRestAdapter",
    "coalesce",
    "extract_items",
    "ratio",
    "resource_id",
    "total",
]
