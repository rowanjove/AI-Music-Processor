import json
import os
import tempfile
import unittest

from core.storage import JobStore, sha256_file, stage_cache_key


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "state.sqlite3")
        self.source = os.path.join(self.temp_dir.name, "source.txt")
        self.output = os.path.join(self.temp_dir.name, "output.wav")
        with open(self.source, "wb") as handle:
            handle.write(b"source")
        with open(self.output, "wb") as handle:
            handle.write(b"output")
        self.store = JobStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_job_stage_and_cache_round_trip(self):
        job_id = self.store.create_job(self.source, "polish", {"preset": "natural"})
        output_hash = sha256_file(self.output)
        self.store.upsert_stage(job_id, "Clean", status="DONE", input_hash="input-hash", outputs={"cleaned": self.output}, output_hashes={"cleaned": output_hash}, parameters={"preset": "natural"})
        stage = self.store.get_stage(job_id, "Clean")
        self.assertEqual(stage["status"], "DONE")
        self.assertEqual(stage["outputs"]["cleaned"], self.output)
        self.assertEqual(stage["output_hashes"]["cleaned"], output_hash)

        key = stage_cache_key("Clean", {"sha256": "source"}, {"preset": "natural"})
        self.store.put_cache(key, "Clean", "input-hash", {"preset": "natural"}, {"cleaned": self.output}, {"cleaned": output_hash})
        cached = self.store.get_cache(key)
        self.assertEqual(cached["outputs"]["cleaned"], self.output)
        self.assertEqual(cached["output_hashes"]["cleaned"], output_hash)

    def test_license_and_output_records_survive_reopen(self):
        job_id = self.store.create_job(self.source, "voice_conversion")
        self.store.register_output(job_id, self.output, kind="Master")
        self.store.put_license("model", "demo", name="Demo", license_name="CC-BY", commercial_status="Warning", metadata={"source": "fixture"})
        self.store.close()
        self.store = JobStore(self.db_path)
        self.assertEqual(self.store.list_outputs(job_id)[0]["sha256"], sha256_file(self.output))
        license_record = self.store.list_licenses("model", "demo")[0]
        self.assertEqual(license_record["commercial_status"], "Warning")
        self.assertEqual(license_record["metadata"], {"source": "fixture"})

    def test_hash_changes_when_file_changes(self):
        before = sha256_file(self.source)
        with open(self.source, "ab") as handle:
            handle.write(b"changed")
        self.assertNotEqual(before, sha256_file(self.source))


if __name__ == "__main__":
    unittest.main()
