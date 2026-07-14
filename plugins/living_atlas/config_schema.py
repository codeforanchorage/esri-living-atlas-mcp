"""Pydantic configuration schema for the Living Atlas plugin."""

import re
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ITEM_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class LivingAtlasPluginConfig(BaseModel):
    """Configuration schema for the ArcGIS Living Atlas plugin.

    The defaults point at the public ArcGIS Online sharing API and the
    Living Atlas curation group, both determined empirically from the
    official browse app (see docs/CATALOG_NOTES.md). No token field is
    defined on purpose: this server is read-only, anonymous, and premium/
    subscriber content is explicitly out of scope.
    """

    enabled: bool = Field(default=False, description="Whether plugin is enabled")
    portal_url: str = Field(
        default="https://www.arcgis.com",
        description="Base URL of ArcGIS Online (sharing REST API host).",
    )
    group_id: str = Field(
        default="47dd57c9a59d458c86d3d6b978560088",
        description=(
            "ArcGIS Online group ID that defines Living Atlas membership. "
            "All catalog queries are scoped to this group."
        ),
    )
    category_set_item_id: str = Field(
        default="1ad6b64fe4e1428a8f182dd6010fc2c9",
        description=(
            "Item ID of the Living Atlas content category set; its /data "
            "holds the category taxonomy served by list_categories."
        ),
    )
    timeout: int = Field(
        default=25,
        ge=1,
        le=300,
        description=(
            "Upstream HTTP timeout in seconds. Keep under 29s: API Gateway "
            "hard-caps responses at 29s, and a clean JSON-RPC timeout error "
            "beats an opaque 504."
        ),
    )
    search_cache_ttl: float = Field(
        default=600.0,
        ge=0,
        description="TTL in seconds for cached search results (~10 min).",
    )
    metadata_cache_ttl: float = Field(
        default=3600.0,
        ge=0,
        description=(
            "TTL in seconds for cached item metadata, service/layer "
            "schemas, and the category taxonomy (~1 h)."
        ),
    )
    max_query_records: int = Field(
        default=500,
        ge=1,
        le=2000,
        description=(
            "Server-side cap on records a single query_data call may "
            "return. Many Living Atlas layers are national/global scale; "
            "this bounds response size regardless of the caller's limit."
        ),
    )

    @field_validator("portal_url")
    @classmethod
    def validate_portal_url(cls, v: str) -> str:
        result = urlparse(v)
        if result.scheme not in ("http", "https") or not result.netloc:
            raise ValueError("portal_url must be an http(s) URL")
        return v.rstrip("/")

    @field_validator("group_id", "category_set_item_id")
    @classmethod
    def validate_item_id(cls, v: str) -> str:
        if not _ITEM_ID_RE.match(v):
            raise ValueError("must be a 32-character hex ArcGIS Online ID")
        return v.lower()

    model_config = ConfigDict(extra="forbid")
