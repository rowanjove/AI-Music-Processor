#!/usr/bin/env python3
"""Inspect test tiers and runtime dependencies for AI Music Processor.

Categorizes tests into:
  - tier1_stdlib: Only relies on Python standard library and in-repo mock/pure logic
  - tier2_dsp: Requires audio/DSP packages (numpy, scipy, soundfile, etc.)
  - tier3_heavy: Requires heavy ML frameworks (torch, faiss, demucs, basic_pitch, etc.)
"""
import argparse
import ast
import os
import platform
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DSP_MODULES = {"numpy", "scipy", "soundfile", "librosa", "resampy"}
HEAVY_MODULES = {"torch", "torchaudio", "faiss", "onnxruntime", "demucs", "basic_pitch"}


def probe_environment():
    status = {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "modules": {},
        "ffmpeg": False,
        "nvidia_smi": False,
    }
    all_probes = sorted(list(DSP_MODULES | HEAVY_MODULES))
    for mod in all_probes:
        try:
            __import__(mod)
            status["modules"][mod] = True
        except ImportError:
            status["modules"][mod] = False

    status["ffmpeg"] = bool(shutil.which("ffmpeg") or (ROOT / "runtime" / "bin" / "ffmpeg.exe").is_file() or (ROOT / "tools" / "RVC" / "RVC-beta-v2-0618" / "ffmpeg.exe").is_file())
    status["nvidia_smi"] = bool(shutil.which("nvidia-smi"))
    return status


def analyze_test_file(test_path: Path):
    with open(test_path, "r", encoding="utf-8", errors="ignore") as f:
        try:
            tree = ast.parse(f.read(), str(test_path))
        except SyntaxError:
            return set()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imports.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
    return imports


def categorize_tests(test_dir: Path):
    test_files = sorted(test_dir.glob("test_*.py"))
    categories = {
        "tier1_stdlib": [],
        "tier2_dsp": [],
        "tier3_heavy": [],
    }

    for tf in test_files:
        imports = analyze_test_file(tf)
        heavy_hits = sorted(imports & HEAVY_MODULES)
        dsp_hits = sorted(imports & DSP_MODULES)

        if heavy_hits:
            categories["tier3_heavy"].append((tf.name, heavy_hits))
        elif dsp_hits:
            categories["tier2_dsp"].append((tf.name, dsp_hits))
        else:
            categories["tier1_stdlib"].append(tf.name)

    return categories


def run_tier(tier_files, test_dir: Path):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(test_dir) not in sys.path:
        sys.path.insert(0, str(test_dir))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for fname in tier_files:
        mod_name = fname[:-3]
        try:
            sub = loader.loadTestsFromName(f"tests.{mod_name}")
            suite.addTests(sub)
        except Exception as exc:
            print(f"Error loading {fname}: {exc}")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description="Inspect test tiers and runtime dependencies.")
    parser.add_argument("--check-env", action="store_true", help="Probe current environment dependencies")
    parser.add_argument("--list", action="store_true", help="List test files by tier")
    parser.add_argument("--run-tier", choices=["tier1", "tier2", "tier3"], help="Run tests for specified tier")
    args = parser.parse_args()

    test_dir = ROOT / "tests"
    cats = categorize_tests(test_dir)

    if args.check_env or (not args.list and not args.run_tier):
        env = probe_environment()
        print(f"=== Environment Probe ===")
        print(f"Python: {env['python_executable']} ({env['python_version']})")
        print(f"Platform: {env['platform']}")
        print(f"FFmpeg available: {env['ffmpeg']}")
        print(f"NVIDIA SMI available: {env['nvidia_smi']}")
        print(f"DSP Modules (Tier 2):")
        for m in sorted(DSP_MODULES):
            state = "AVAILABLE" if env["modules"].get(m) else "MISSING"
            print(f"  - {m}: {state}")
        print(f"Heavy ML Modules (Tier 3):")
        for m in sorted(HEAVY_MODULES):
            state = "AVAILABLE" if env["modules"].get(m) else "MISSING"
            print(f"  - {m}: {state}")

    if args.list or (not args.check_env and not args.run_tier):
        print(f"\n=== Test Categorization ===")
        print(f"Tier 1 (stdlib-only / mock): {len(cats['tier1_stdlib'])} files")
        for name in cats["tier1_stdlib"]:
            print(f"  [T1] {name}")
        print(f"\nTier 2 (DSP / audio packages): {len(cats['tier2_dsp'])} files")
        for name, deps in cats["tier2_dsp"]:
            print(f"  [T2] {name} (requires: {', '.join(deps)})")
        print(f"\nTier 3 (Heavy ML / frameworks): {len(cats['tier3_heavy'])} files")
        for name, deps in cats["tier3_heavy"]:
            print(f"  [T3] {name} (requires: {', '.join(deps)})")

    if args.run_tier:
        tier_key = f"{args.run_tier}_{'stdlib' if args.run_tier == 'tier1' else 'dsp' if args.run_tier == 'tier2' else 'heavy'}"
        files = cats[tier_key]
        if args.run_tier in ("tier2", "tier3"):
            file_names = [f[0] for f in files]
        else:
            file_names = files
        print(f"\nRunning {args.run_tier} ({len(file_names)} files)...")
        sys.exit(run_tier(file_names, test_dir))


if __name__ == "__main__":
    main()
