"""Tests for config/common/openapi-compat.jq. Run: python3 -m unittest discover -s factory/scripts"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path

COMPAT = Path(__file__).resolve().parents[2] / "config" / "common" / "openapi-compat.jq"
LIST, STREAMED = "/stores/{store_id}/list-objects", "/stores/{store_id}/streamed-list-objects"


def body(context_doc: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "example": "document"},
            "consistency": {"$ref": "#/definitions/ConsistencyPreference"},
            "context": {"type": "object", "description": context_doc},
        },
        "required": ["type"],
    }


def operation(operation_id: str, schema: dict) -> dict:
    return {
        "post": {
            "operationId": operation_id,
            "tags": ["OpenFGAService"],
            "parameters": [
                {"name": "store_id", "in": "path", "required": True, "type": "string"},
                {"name": "body", "in": "body", "required": True, "schema": schema},
            ],
        }
    }


def document(list_schema: dict, streamed_schema: dict, definitions: dict) -> dict:
    return {
        "swagger": "2.0",
        "paths": {LIST: operation("ListObjects", list_schema), STREAMED: operation("StreamedListObjects", streamed_schema)},
        "definitions": {"ConsistencyPreference": {"type": "string", "enum": ["UNSPECIFIED"]}, **definitions},
    }


def named(streamed_body: dict) -> dict:
    return document(
        {"$ref": "#/definitions/ListObjectsBody"},
        {"$ref": "#/definitions/StreamedListObjectsBody"},
        {"ListObjectsBody": body("Additional request context that are used to evaluate."), "StreamedListObjectsBody": streamed_body},
    )


def body_ref(doc: dict, path: str) -> str:
    return next(p for p in doc["paths"][path]["post"]["parameters"] if p["in"] == "body")["schema"]["$ref"]


@unittest.skipUnless(shutil.which("jq"), "needs jq")
class OpenApiCompatTest(unittest.TestCase):
    def compat(self, doc: dict) -> subprocess.CompletedProcess:
        return subprocess.run(["jq", "-f", str(COMPAT)], input=json.dumps(doc), capture_output=True, text=True)

    def test_streamed_body_shares_the_list_objects_model(self):
        result = self.compat(named(body("Additional request context used to evaluate.")))
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        self.assertEqual(body_ref(doc, LIST), "#/definitions/ListObjectsRequest")
        self.assertEqual(body_ref(doc, STREAMED), "#/definitions/ListObjectsRequest")
        self.assertEqual(sorted(doc["definitions"]), ["ConsistencyPreference", "ListObjectsRequest"])

    def test_streamed_body_with_a_new_field_stops_the_build(self):
        streamed = body("Additional request context used to evaluate.")
        streamed["properties"]["page_size"] = {"type": "integer", "format": "int32"}
        result = self.compat(named(streamed))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("StreamedListObjectsBody no longer matches ListObjectsBody", result.stderr)

    def test_inline_bodies_are_unchanged(self):
        inline = body("Additional request context that are used to evaluate.")
        doc = document(inline, copy.deepcopy(inline), {})
        result = self.compat(doc)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), doc)


if __name__ == "__main__":
    unittest.main()
