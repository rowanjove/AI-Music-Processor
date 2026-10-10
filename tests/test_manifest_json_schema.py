"""Validate generated manifests with the shipped JSON schemas, offline."""
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, RefResolver

ROOT = Path(__file__).resolve().parents[1]


class ManifestSchemaTests(unittest.TestCase):
    def test_installed_manifests_match_their_schemas(self):
        base = ROOT / "runtime-manifest"
        schemas = {path.stem: json.loads(path.read_text(encoding="utf-8"))
                   for path in (base / "schema").glob("*.json")}
        store = {schema["$id"]: schema for schema in schemas.values()}
        for name, filename in (("runtime", "runtime.json"), ("inventory", "inventory.json"),
                               ("models", "models.json"), ("packages", "packages.lock.json"),
                               ("vendor", "vendor.json"), ("components", "components.json")):
            with self.subTest(manifest=filename):
                schema = schemas[name + ".schema"]
                validator = Draft202012Validator(schema,
                    resolver=RefResolver.from_schema(schema, store=store))
                errors = list(validator.iter_errors(json.loads((base / filename).read_text(encoding="utf-8"))))
                self.assertEqual(errors, [])

    def test_artifact_record_does_not_require_inventory_roles(self):
        base = ROOT / "runtime-manifest/schema"
        schema = json.loads((base / "inventory.schema.json").read_text(encoding="utf-8"))
        artifact = schema["$defs"]["artifact_file"]
        validator = Draft202012Validator(artifact)
        record = {"path": "models/model.pt", "size": 10, "sha256": "a" * 64}
        validator.validate(record)
        self.assertTrue(list(validator.iter_errors(dict(record, path="../outside.pt"))))
        self.assertTrue(list(validator.iter_errors(dict(record, path="C:/outside.pt"))))


if __name__ == "__main__":
    unittest.main()
