#!/usr/bin/env python3
"""Opt-in destructive tests for a *disposable* local Homebox, not the pytest suite."""
import os
import sys
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def local_target():
    url = os.environ.get("HBOX_INTEGRATION_URL", "")
    if os.environ.get("HBOX_INTEGRATION_DISPOSABLE") != "yes":
        raise SystemExit("Set HBOX_INTEGRATION_DISPOSABLE=yes for a throwaway backend")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise SystemExit(f"Invalid URL: {exc}") from exc
    if (parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1"}
            or not port or parts.username or parts.password or parts.path not in {"", "/"}
            or parts.query or parts.fragment):
        raise SystemExit("URL must be http://localhost:PORT or http://127.0.0.1:PORT")
    # Never let localhost resolution or HTTP proxy settings route this to another machine.
    return f"http://127.0.0.1:{port}/api/v1"


API = local_target()
for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(key, None)
os.environ["HOMEBOX_URL"] = API.removesuffix("/api/v1")
os.environ["HOMEBOX_TOKEN"] = "disposable-placeholder"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import homebox_mcp as hb  # noqa: E402
from homebox_mcp_models import EntityChanges  # noqa: E402


class LiveRoundtrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = httpx.Client(base_url=API, trust_env=False, follow_redirects=False, timeout=15)
        email = f"integration-{uuid.uuid4().hex}@example.invalid"
        password = "disposable-test-password"
        cls.request("POST", "/users/register", {"name": "Disposable integration", "email": email,
                                                 "password": password})
        token = cls.request("POST", "/users/login", {"username": email, "password": password})["token"]
        cls.client.headers["Authorization"] = token
        key = cls.request("POST", "/users/self/api-keys", {"name": "disposable integration"})["token"]
        cls.client.headers["Authorization"] = f"Bearer {key}"
        cls.original_client = hb._client
        hb._client = cls.client
        hb._version_checked = False
        types = cls.request("GET", "/entity-types")
        cls.item_type = next(t["id"] for t in types if not t["isLocation"])
        cls.location_type = next(t["id"] for t in types if t["isLocation"])

    @classmethod
    def tearDownClass(cls):
        hb._client = cls.original_client
        cls.client.close()

    @classmethod
    def request(cls, method, path, body=None):
        response = cls.client.request(method, path, json=body)
        response.raise_for_status()
        return response.json() if response.content else None

    def create(self, name, typ, **extra):
        return self.request("POST", "/entities", {"name": name, "entityTypeId": typ,
                                                   "quantity": 1, **extra})["id"]

    def raw(self, entity_id):
        return self.request("GET", f"/entities/{entity_id}")

    def update(self, entity_id, **changes):
        return hb.update_entity(uuid.UUID(entity_id), EntityChanges(**changes))

    def test_real_roundtrip_and_preservation(self):
        location_a = self.create("integration location A", self.location_type)
        location_b = self.create("integration location B", self.location_type)
        nested_location = self.create("integration nested location", self.location_type,
                                      parentId=location_a)
        with self.assertRaises(hb.ToolError):
            self.update(location_a, locationId=nested_location)
        self.assertFalse(self.raw(location_a).get("parent"))
        parent = self.create("integration parent", self.item_type, parentId=location_a)
        child = self.create("integration duplicate", self.item_type, parentId=parent,
                            locationId=location_b, quantity=1.25)
        duplicate = self.create("integration duplicate", self.item_type, parentId=location_a)
        inherited = self.create("integration inherited", self.item_type, parentId=parent)
        self.assertFalse(self.raw(inherited).get("locationId"))
        before = self.raw(child)
        self.assertEqual(before["locationId"], location_b)
        self.assertEqual(before["quantity"], 1.25)
        self.assertEqual(str(hb.get_entity(uuid.UUID(child)).id), child)
        self.update(child, description="roundtrip", insured=True, purchasePrice=12.5,
                    purchaseDate="2025-01-02T00:00:00Z",
                    fields=[{"name": "integration field", "textValue": "old"}])
        first = self.raw(child)
        field = first["fields"][0]
        self.assertTrue(field["id"])
        self.assertEqual(first["locationId"], location_b)
        self.update(child, name="integration renamed", insured=False, purchasePrice=0,
                    fields=[{**field, "name": "integration field renamed", "textValue": "new"}])
        after = self.raw(child)
        self.assertEqual(after["fields"][0]["id"], field["id"])
        self.assertEqual(after["fields"][0]["name"], "integration field renamed")
        self.assertEqual(after["fields"][0]["textValue"], "new")
        self.assertEqual(after["locationId"], location_b)
        self.assertEqual(after["quantity"], 1.25)
        self.assertFalse(after["insured"])
        self.assertEqual(after["purchasePrice"], 0)
        self.assertEqual(self.raw(duplicate)["name"], "integration duplicate")
        scalars = {"manufacturer": "Fixture maker", "modelNumber": "Model",
                   "serialNumber": "Serial", "notes": "fixture notes",
                   "purchaseFrom": "Fixture shop", "warrantyDetails": "Fixture warranty",
                   "lifetimeWarranty": True, "soldTo": "Fixture buyer", "soldPrice": 10,
                   "soldNotes": "Fixture sale", "archived": True, "assetId": "005-123"}
        self.update(child, **scalars, warrantyExpires="2027-01-01", soldDate="2025-02-01")
        scalar_after = self.raw(child)
        for key, value in scalars.items():
            self.assertEqual(scalar_after[key], value, key)
        self.assertTrue(scalar_after["warrantyExpires"].startswith("2027-01-01"))
        self.assertTrue(scalar_after["soldDate"].startswith("2025-02-01"))
        self.update(child, quantity=2.5)
        self.assertEqual(self.raw(child)["notes"], "fixture notes")
        self.assertEqual(self.raw(child)["quantity"], 2.5)
        self.update(child, locationId=None)
        self.assertFalse(self.raw(child).get("locationId"))
        self.assertEqual(self.raw(child)["location"]["id"], location_a)
        self.update(child, locationId=location_b)
        self.assertEqual(self.raw(child)["locationId"], location_b)
        self.update(child, fields=[], purchaseDate=None)
        cleared = self.raw(child)
        self.assertFalse(cleared["fields"])
        self.assertFalse(cleared.get("purchaseDate"))
        self.update(parent, syncChildEntityLocations=True)
        self.assertFalse(self.raw(child).get("locationId"))
        self.update(parent, description="preserve sync")
        self.assertTrue(self.raw(parent)["syncChildEntityLocations"])
        self.assertFalse(self.raw(inherited).get("locationId"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
