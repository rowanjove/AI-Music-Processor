import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_lightweight import build, source_files


class LightweightDeliveryTests(unittest.TestCase):
    def test_source_inventory_excludes_large_runtime_and_user_trees(self):
        paths = {relative.casefold() for relative, _path in source_files(ROOT)}
        self.assertIn("core/online_components.py", paths)
        self.assertIn("scripts/install_components.ps1", paths)
        for forbidden in ("runtime/", "tools/", "models/", "output/", "song/", "config/", "dist/"):
            self.assertFalse(any(item == forbidden or item.startswith(forbidden) for item in paths), forbidden)
        binary_paths = {item for item in paths if item.endswith(('.exe', '.dll', '.pyd', '.pth', '.pt', '.onnx', '.wav', '.mp3', '.zip'))}
        self.assertTrue(binary_paths.issubset({'ai-music-processor.exe'}), binary_paths)

    def test_build_is_deterministic_enough_and_manifest_hashes_match(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "AI-Music-Processor-source.zip"
            result = build(output, ROOT)
            self.assertEqual(result["status"], "CREATED")
            self.assertLess(result["zip_bytes"], 100 * 1024 * 1024)
            self.assertTrue(Path(result["sha256sums"]).is_file())
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read("lightweight-manifest.json"))
                names = {name.casefold() for name in archive.namelist()}
                self.assertIn("lightweight_release", names)
                self.assertIn("portable_release", names)
                self.assertEqual(len(names), len(archive.namelist()))
                for record in manifest["files"]:
                    payload = archive.read(record["path"])
                    self.assertEqual(len(payload), record["size"], record["path"])
                    self.assertEqual(hashlib.sha256(payload).hexdigest(), record["sha256"], record["path"])
                self.assertNotIn("tools/rvc/rvc-beta-v2-0618/runtime/python.exe", names)

    def test_clean_extract_and_comprehensive_asset_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "AI-Music-Processor-clean-test.zip"
            result = build(output, ROOT)
            self.assertEqual(result["status"], "CREATED")
            sums_path = Path(result["sha256sums"])
            self.assertTrue(sums_path.is_file())
            sums_content = sums_path.read_text(encoding="utf-8")
            self.assertIn(result["sha256"], sums_content)

            extract_dir = Path(directory) / "extracted"
            with zipfile.ZipFile(output) as archive:
                archive.extractall(extract_dir)

            # 1. Native bootstrap executable check
            launcher_exe = extract_dir / "AI-Music-Processor.exe"
            self.assertTrue(launcher_exe.is_file())
            exe_bytes = launcher_exe.read_bytes()
            self.assertTrue(exe_bytes.startswith(b"MZ"), "Launcher must be a valid Windows PE binary")
            self.assertGreater(len(exe_bytes), 1024)

            # 2. Markers and manifests
            lw_marker = extract_dir / "LIGHTWEIGHT_RELEASE"
            self.assertTrue(lw_marker.is_file())
            self.assertIn("Lightweight Release", lw_marker.read_text(encoding="utf-8-sig"))

            portable_marker = extract_dir / "PORTABLE_RELEASE"
            self.assertTrue(portable_marker.is_file())

            # 3. Version consistency across package
            version_text = (extract_dir / "VERSION").read_text(encoding="utf-8-sig").strip()
            manifest = json.loads((extract_dir / "lightweight-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], version_text)
            sources = json.loads((extract_dir / "packaging" / "download-sources.json").read_text(encoding="utf-8"))
            self.assertEqual(sources["app_version"], version_text)

            # 4. Strict exclusion of heavy assets and user data
            for path in extract_dir.rglob("*"):
                if path.is_file():
                    ext = path.suffix.casefold()
                    self.assertNotIn(ext, {".pth", ".pt", ".onnx", ".index", ".wav", ".mp3", ".flac"})
                    rel = path.relative_to(extract_dir).as_posix()
                    self.assertFalse(any(part in {"runtime", "tools", "models", "output", "song", "config", "logs"}
                                         for part in Path(rel).parts), rel)

    def test_extracted_modular_runtime_passes_second_run_without_blockers(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "AI-Music-Processor-modular-test.zip"
            build(output, ROOT)
            extract_dir = Path(directory) / "extracted"
            with zipfile.ZipFile(output) as archive:
                archive.extractall(extract_dir)

            from core.runtime_verify import verify_runtime
            data_root = Path(directory) / "data_root"

            # Before install: verify_runtime fails closed
            res_before = verify_runtime(root=extract_dir, data_root=data_root)
            self.assertEqual(res_before["status"], "FAIL")

            # Simulate Polish install: add runtime files and bootstrap manifest
            runtime = "tools/RVC/RVC-beta-v2-0618/runtime"
            site = f"{runtime}/Lib/site-packages"
            mock_runtime_files = [
                f"{runtime}/python.exe",
                f"{site}/numpy/__init__.py",
                f"{site}/scipy/__init__.py",
                f"{site}/soundfile.py",
                "runtime/bin/ffmpeg.exe",
                "runtime/bin/ffprobe.exe",
            ]
            for rel in mock_runtime_files:
                target = extract_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"mock " + rel.encode())

            inventory_roots = [
                extract_dir / "AI_Music_Processor.pyw",
                extract_dir / "process_music.py",
                extract_dir / "VERSION",
                extract_dir / "core",
                extract_dir / "runtime",
                extract_dir / "tools" / "RVC" / "RVC-beta-v2-0618" / "runtime",
            ]
            file_records = []
            for base in inventory_roots:
                if not base.exists():
                    continue
                paths = [base] if base.is_file() else list(base.rglob("*"))
                for path in paths:
                    if not path.is_file() or "__pycache__" in path.parts or path.suffix.casefold() in {".pyc", ".pyo"}:
                        continue
                    relative = path.relative_to(extract_dir).as_posix()
                    file_records.append({"path": relative, "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

            bootstrap = {
                "schema_version": 1,
                "profile": "modular-polish-v1",
                "allowed_modes": ["polish"],
                "files": file_records,
                "python": {"executable": f"{runtime}/python.exe", "version": "3.9.13"},
                "tools": {"ffmpeg": {"version_line": "ffmpeg 6.0"}, "ffprobe": {"version_line": "ffprobe 6.0"}},
            }
            (extract_dir / "runtime-manifest").mkdir(exist_ok=True)
            (extract_dir / "runtime-manifest" / "bootstrap-runtime.json").write_text(json.dumps(bootstrap), encoding="utf-8")

            from unittest.mock import Mock, patch
            def mock_popen(command, *args, **kwargs):
                cmd = " ".join(str(c) for c in command)
                m = Mock()
                m.poll.return_value = 0
                m.returncode = 0
                if "python.exe" in cmd:
                    modules = {
                        "numpy": str(extract_dir / f"{site}/numpy/__init__.py"),
                        "scipy": str(extract_dir / f"{site}/scipy/__init__.py"),
                        "soundfile": str(extract_dir / f"{site}/soundfile.py"),
                    }
                    m.communicate.return_value = (json.dumps({"version": "3.9.13", "modules": modules}).encode(), b"")
                elif "ffmpeg.exe" in cmd:
                    m.communicate.return_value = (b"ffmpeg 6.0\n", b"")
                elif "ffprobe.exe" in cmd:
                    m.communicate.return_value = (b"ffprobe 6.0\n", b"")
                return m

            with patch("core.modular_runtime.subprocess.Popen", side_effect=mock_popen):
                res_after = verify_runtime(root=extract_dir, data_root=data_root)
                self.assertEqual(res_after["status"], "PASS")
                self.assertEqual(res_after["allowed_modes"], ["polish"])
                self.assertEqual(res_after["profile"], "modular-polish-v1")
                self.assertTrue((extract_dir / "PORTABLE_RELEASE").is_file())

    def test_readme_and_doc_links_point_to_real_files(self):
        import re
        readme_path = ROOT / "README.md"
        self.assertTrue(readme_path.is_file())
        content = readme_path.read_text(encoding="utf-8")
        links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", content)
        for _text, link in links:
            if link.startswith(("http://", "https://", "#")):
                continue
            target = ROOT / link
            self.assertTrue(target.exists(), f"README link {link} does not exist on disk")


if __name__ == "__main__":
    unittest.main()
