"""Resource classes: one per API area. They validate inputs and build operations; only the pipeline sends."""

from .misc import (
    AsyncCreditsResource,
    AsyncHealthResource,
    AsyncSyncLinksResource,
    AsyncTaxonomiesResource,
    CreditsResource,
    HealthResource,
    SyncLinksResource,
    TaxonomiesResource,
)
from .tracks import AsyncTracksResource, TracksResource

__all__ = [
    "AsyncCreditsResource",
    "AsyncHealthResource",
    "AsyncSyncLinksResource",
    "AsyncTaxonomiesResource",
    "AsyncTracksResource",
    "CreditsResource",
    "HealthResource",
    "SyncLinksResource",
    "TaxonomiesResource",
    "TracksResource",
]
