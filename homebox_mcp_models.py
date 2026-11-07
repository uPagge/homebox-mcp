"""Typed inventory wire contracts. Omitted update members never mean clear."""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)


class EntityField(BaseModel):
    """A custom-field DTO, retaining its stable backend identifier."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: UUID | None = None
    name: Annotated[str, Field(min_length=1, max_length=255)]
    type: Literal["text", "number", "boolean", "time"] = "text"
    text_value: str = Field(default="", alias="textValue", max_length=500)
    number_value: StrictInt = Field(default=0, alias="numberValue")
    boolean_value: StrictBool = Field(default=False, alias="booleanValue")


class EntityOut(BaseModel):
    """Lossless backend entity, including additional version-specific metadata."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    id: UUID
    name: str
    asset_id: str = Field(default="", alias="assetId")
    description: str = ""
    notes: str = ""
    quantity: FiniteFloat = 0
    insured: bool = False
    archived: bool = False
    manufacturer: str = ""
    model_number: str = Field(default="", alias="modelNumber")
    serial_number: str = Field(default="", alias="serialNumber")
    purchase_price: FiniteFloat = Field(default=0, alias="purchasePrice")
    purchase_date: str | None = Field(default=None, alias="purchaseDate")
    purchase_from: str = Field(default="", alias="purchaseFrom")
    sold_price: FiniteFloat = Field(default=0, alias="soldPrice")
    sold_date: str | None = Field(default=None, alias="soldDate")
    sold_to: str = Field(default="", alias="soldTo")
    sold_notes: str = Field(default="", alias="soldNotes")
    lifetime_warranty: bool = Field(default=False, alias="lifetimeWarranty")
    warranty_expires: str | None = Field(default=None, alias="warrantyExpires")
    warranty_details: str = Field(default="", alias="warrantyDetails")
    sync_child_entity_locations: bool = Field(default=False, alias="syncChildEntityLocations")
    location_id: UUID | None = Field(default=None, alias="locationId")
    location: dict[str, Any] | None = None
    parent: dict[str, Any] | None = None
    entity_type: dict[str, Any] | None = Field(default=None, alias="entityType")
    tags: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[EntityField] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None = Field(default=None, alias="createdAt")
    updated_at: str | None = Field(default=None, alias="updatedAt")
    image_id: UUID | None = Field(default=None, alias="imageId")
    thumbnail_id: UUID | None = Field(default=None, alias="thumbnailId")
    children: list[dict[str, Any]] = Field(default_factory=list)
    item_count: FiniteFloat | None = Field(default=None, alias="itemCount")
    total_price: FiniteFloat | None = Field(default=None, alias="totalPrice")


class ItemDetail(BaseModel):
    """Backward-compatible detail view; typed fields also available losslessly."""
    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    custom_fields: list[dict[str, JsonValue]] = Field(default_factory=list)
    location_path: str | None = None


class EntityChanges(BaseModel):
    """Partial entity edit. null clears nullable dates/relations; [] clears lists.

    fields replaces the complete field list; use remove_field_ids for surgical
    deletion. Existing field IDs must belong to the entity. System timestamps,
    attachments and resolved physical location are not arbitrary scalar edits.
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    description: Annotated[str, Field(max_length=1000)] | None = None
    notes: Annotated[str, Field(max_length=10000)] | None = None
    quantity: Annotated[FiniteFloat, Field(ge=0)] | None = None
    insured: StrictBool | None = None
    archived: StrictBool | None = None
    manufacturer: Annotated[str, Field(max_length=255)] | None = None
    model_number: str | None = Field(default=None, alias="modelNumber", max_length=255)
    serial_number: str | None = Field(default=None, alias="serialNumber", max_length=255)
    purchase_price: FiniteFloat | None = Field(default=None, alias="purchasePrice")
    purchase_date: str | None = Field(default=None, alias="purchaseDate")
    purchase_from: str | None = Field(default=None, alias="purchaseFrom", max_length=255)
    sold_price: FiniteFloat | None = Field(default=None, alias="soldPrice")
    sold_date: str | None = Field(default=None, alias="soldDate")
    sold_to: str | None = Field(default=None, alias="soldTo", max_length=255)
    sold_notes: str | None = Field(default=None, alias="soldNotes", max_length=10000)
    lifetime_warranty: StrictBool | None = Field(default=None, alias="lifetimeWarranty")
    warranty_expires: str | None = Field(default=None, alias="warrantyExpires")
    warranty_details: str | None = Field(default=None, alias="warrantyDetails", max_length=10000)
    sync_child_entity_locations: StrictBool | None = Field(
        default=None, alias="syncChildEntityLocations")
    parent_id: UUID | None = Field(default=None, alias="parentId")
    location_id: UUID | None = Field(default=None, alias="locationId")
    entity_type_id: UUID | None = Field(default=None, alias="entityTypeId")
    asset_id: str | None = Field(default=None, alias="assetId", pattern=r"^\d{3}-\d{3,}$")
    tag_ids: list[UUID] | None = Field(default=None, alias="tagIds", max_length=200)
    fields: list[EntityField] | None = Field(default=None, max_length=200)
    remove_field_ids: list[UUID] = Field(default_factory=list, max_length=200)

    @field_validator("purchase_date", "sold_date", "warranty_expires")
    @classmethod
    def valid_date(cls, value):
        if value in (None, ""):
            return value
        try:
            date.fromisoformat(value)
        except ValueError:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp must include timezone")
        return value

    @model_validator(mode="after")
    def explicit_null_contract(self):
        nullable = {"purchase_date", "sold_date", "warranty_expires", "parent_id", "location_id"}
        for name in self.model_fields_set - nullable:
            if getattr(self, name) is None:
                raise ValueError(f"{name}: null is not a clear operation; use empty string/list or false")
        if "fields" in self.model_fields_set and self.remove_field_ids:
            raise ValueError("use either fields replacement or remove_field_ids")
        if self.fields is not None:
            ids = [str(f.id) for f in self.fields if f.id]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate custom-field IDs")
        return self
