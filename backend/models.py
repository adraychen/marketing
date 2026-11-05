from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Asset(BaseModel):
    text: str = Field(max_length=500)
    pin: str = Field(default="", max_length=30)


class Creative(BaseModel):
    headlines: list[Asset] = Field(default_factory=list, max_length=30)
    descriptions: list[Asset] = Field(default_factory=list, max_length=10)
    final_urls: list[str] = Field(default_factory=list, max_length=20)
    path1: str = Field(default="", max_length=100)
    path2: str = Field(default="", max_length=100)


class Account(BaseModel):
    resource: str
    name: str
    currency: str
    timezone: str
    platform: str = "Google Ads"
    manager: str = ""


class Resource(BaseModel):
    resource: str
    parent: str | None = None
    data: dict


class Daily(BaseModel):
    kind: Literal["campaigns", "keywords", "search_terms"]
    resource: str
    day: date
    currency: str
    spend: float
    impressions: int
    clicks: int
    interactions: int
    conversions: float
    value: float | None


class ImportBatch(BaseModel):
    campaigns: list[Resource] = Field(default_factory=list)
    ad_groups: list[Resource] = Field(default_factory=list)
    ads: list[Resource] = Field(default_factory=list)
    keywords: list[Resource] = Field(default_factory=list)
    search_terms: list[Resource] = Field(default_factory=list)
    performance: list[Daily] = Field(default_factory=list)


class DraftInput(BaseModel):
    creative: Creative
    version: int | None = Field(default=None, ge=1)
