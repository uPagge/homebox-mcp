"""Regression contracts for the downstream fork; no real inventory access."""
import asyncio
import copy
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import homebox_mcp as hb

ID = "11111111-1111-4111-8111-111111111111"
FIELD_ID = "22222222-2222-4222-8222-222222222222"
LOCATION_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def no_version_network(monkeypatch):
    monkeypatch.setattr(hb, "_check_min_version", lambda: None)


def entity():
    return {
        "id": ID, "assetId": "000-123", "name": "Fixture", "description": "Details",
        "insured": False, "archived": False, "quantity": 1.5,
        "purchasePrice": 0, "purchaseDate": "", "soldPrice": 0,
        "soldTo": "Buyer", "soldDate": "", "soldNotes": "Sale notes",
        "warrantyDetails": "Terms", "lifetimeWarranty": False,
        "parent": {"id": LOCATION_ID, "name": "Container"},
        "location": {"id": LOCATION_ID, "name": "Physical location"},
        "locationId": LOCATION_ID, "syncChildEntityLocations": True,
        "fields": [{"id": FIELD_ID, "name": "Count", "type": "number", "numberValue": 7}],
        "tags": [], "attachments": [],
    }


def test_domain_error_is_execution_error(monkeypatch):
    monkeypatch.setattr(hb, "_resolve_exact", lambda _: (None, "ambiguous"))
    with pytest.raises(ToolError, match="ambiguous"):
        hb.set_item("ambiguous", new_name="Updated")


def test_item_read_is_lossless_and_retains_legacy_fields(monkeypatch):
    full = entity()
    monkeypatch.setattr(hb, "_resolve_fuzzy", lambda _: full)
    monkeypatch.setattr(hb, "_location_path", lambda _: "Container path")
    result = hb.get_item("000-123")
    for key in ("description", "insured", "archived", "purchasePrice", "soldPrice",
                "soldTo", "soldNotes", "warrantyDetails", "lifetimeWarranty",
                "locationId", "syncChildEntityLocations", "location", "parent"):
        assert result[key] == full[key]
    assert result["fields"] == {"Count": 7}  # legacy name/value view
    assert result["custom_fields"] == full["fields"]
    assert result["location_path"] == "Container path"


def test_preserve_helper_keeps_location_flags_and_field_ids():
    full = entity()
    body = hb._preserve_item_body(full)
    assert body["locationId"] == LOCATION_ID
    assert body["syncChildEntityLocations"] is True
    assert body["fields"][0]["id"] == FIELD_ID
    hb._upsert_field(body, "Count", 8, "number")
    assert body["fields"][0]["id"] == FIELD_ID
    assert full["fields"][0]["numberValue"] == 7


def test_typed_update_roundtrip_and_clear(monkeypatch):
    from homebox_mcp_models import EntityChanges
    full = entity()
    monkeypatch.setattr(hb, "_get", lambda path, **kwargs: copy.deepcopy(full))
    with patch.object(hb, "_put") as put:
        hb.update_entity(ID, EntityChanges(name="Updated", quantity=0.5,
                                          purchaseDate=None, fields=[]))
    body = put.call_args.args[1]
    assert body["quantity"] == 0.5
    assert body["purchaseDate"] == ""
    assert body["fields"] == []
    assert body["name"] == "Updated"
    assert body["locationId"] == LOCATION_ID
    assert body["syncChildEntityLocations"] is True
    assert body["soldTo"] == "Buyer"


@pytest.mark.parametrize("changes", [
    {"locationId": LOCATION_ID}, {"parentId": None, "locationId": LOCATION_ID},
])
def test_location_override_cannot_create_containment_cycle(monkeypatch, changes):
    from homebox_mcp_models import EntityChanges
    full = {"id": ID, "name": "Root", "entityType": {"isLocation": True}, "fields": []}
    monkeypatch.setattr(hb, "_get", lambda *args, **kwargs: copy.deepcopy(full))
    monkeypatch.setattr(hb, "_path_nodes", lambda _: [{"id": ID}, {"id": LOCATION_ID}])
    with patch.object(hb, "_put") as put:
        with pytest.raises(ToolError, match="descendant|parent"):
            hb.update_entity(ID, EntityChanges(**changes))
    put.assert_not_called()


def test_default_custom_field_type_is_sent_before_mutation(monkeypatch):
    from homebox_mcp_models import EntityChanges
    full = entity()
    monkeypatch.setattr(hb, "_get", lambda *args, **kwargs: copy.deepcopy(full))
    with patch.object(hb, "_put") as put:
        hb.update_entity(ID, EntityChanges(name="Updated", fields=[{"name": "Comment", "textValue": "new"}]))
    assert put.call_args.args[1]["fields"][0]["type"] == "text"


def test_quantity_update_uses_patch_not_stale_put(monkeypatch):
    from homebox_mcp_models import EntityChanges
    full = entity()
    monkeypatch.setattr(hb, "_get", lambda *args, **kwargs: copy.deepcopy(full))
    with patch.object(hb, "_put") as put, patch.object(hb, "_patch") as partial:
        hb.update_entity(ID, EntityChanges(quantity=0.25))
    put.assert_not_called()
    partial.assert_called_once_with(f"/entities/{ID}", {"id": ID, "quantity": 0.25})


def test_get_entity_has_typed_schema():
    tools = asyncio.run(hb.mcp.list_tools())
    tool = next(t for t in tools if t.name == "get_entity")
    assert tool.output_schema is not None


def test_unknown_update_key_and_nonfinite_values_rejected():
    from pydantic import ValidationError

    from homebox_mcp_models import EntityChanges
    with pytest.raises(ValidationError):
        EntityChanges.model_validate({"typo": "not silently ignored"})
    with pytest.raises(ValidationError):
        EntityChanges(quantity=float("nan"))


def test_number_field_does_not_silently_truncate():
    with pytest.raises((ToolError, ValueError), match="integer|whole"):
        hb._make_field("Count", 1.5, "number")
