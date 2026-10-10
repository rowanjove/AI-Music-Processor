#!/usr/bin/env python3
"""Fast CI validation script for AI Music Processor.

Runs fast validation without requiring heavy ML frameworks or GPU:
  1. Compiles all python files (.py and .pyw) with py_compile
  2. Validates syntax of all JSON files in the repository
  3. Checks that no heavy binary model weights (.pth, .pt, .onnx, .index) are tracked
  4. Runs Tier 1 stdlib tests using inspect_test_tiers.py
"""
import ast
import json
import os
import py_compile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {".git", ".system_generated", "output", ".work", "node_modules", "tmp", "temp", ".venv", "venv", "tools", "runtime"}
FORBIDDEN_EXTENSIONS = {".pth", ".pt", ".onnx", ".index"}


def check_python_syntax():
    print("[1/4] Checking Python syntax for all .py and .pyw files...")
    py_files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for f in filenames:
            if f.endswith((".py", ".pyw")):
                py_files.append(Path(dirpath) / f)

    failed = []
    for path in py_files:
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as exc:
            print(f"  [SYNTAX ERROR] {path.relative_to(ROOT)}: {exc}")
            failed.append(path)

    if failed:
        print(f"FAILED: {len(failed)} files have Python syntax errors.")
        return False
    print(f"  OK: Compiled {len(py_files)} Python files successfully.")
    return True


def check_json_syntax():
    print("[2/4] Validating JSON files...")
    json_files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for f in filenames:
            if f.endswith(".json"):
                json_files.append(Path(dirpath) / f)

    failed = []
    for path in json_files:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                json.load(f)
        except Exception as exc:
            print(f"  [JSON ERROR] {path.relative_to(ROOT)}: {exc}")
            failed.append(path)

    if failed:
        print(f"FAILED: {len(failed)} files have JSON syntax errors.")
        return False
    print(f"  OK: Parsed {len(json_files)} JSON files successfully.")
    return True


def check_forbidden_assets():
    print("[3/4] Checking repository tree for unapproved model weights...")
    violations = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        # Ignore tools/RVC/ directory if present in local dev environment
        rel_dir = os.path.relpath(dirpath, ROOT)
        if rel_dir.startswith("tools" + os.sep) or rel_dir.startswith("tools/"):
            continue
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext in FORBIDDEN_EXTENSIONS:
                violations.append(Path(dirpath) / f)

    if violations:
        print(f"FAILED: Found {len(violations)} forbidden binary weight files in source tree:")
        for v in violations:
            print(f"  {v.relative_to(ROOT)}")
        return False
    print("  OK: No forbidden model weights in tracked repository tree.")
    return True


def run_tier1_tests():
    print("[4/4] Running Tier 1 (stdlib-only) tests...")
    script = ROOT / "scripts" / "inspect_test_tiers.py"
    cmd = [sys.executable, str(script), "--run-tier", "tier1"]
    res = subprocess.run(cmd, cwd=str(ROOT))
    if res.returncode != 0:
        print(f"FAILED: Tier 1 tests failed with exit code {res.returncode}.")
        return False
    print("  OK: Tier 1 tests passed successfully.")
    return True


def main():
    print(f"=== Fast CI Validation ({ROOT}) ===")
    steps = [
        check_python_syntax,
        check_json_syntax,
        check_forbidden_assets,
        run_tier1_tests,
    ]
    for step in steps:
        if not step():
            print("\n>>> FAST CI: FAILED <<<")
            sys.exit(1)
    print("\n>>> FAST CI: ALL PASSED <<<")
    sys.exit(0)


if __name__ == "__main__":
    main()
