#!/usr/bin/env python3
"""Record a conservative preflight from an extracted Portable package.

This runner validates the immutable runtime and captures host facts. It does
not claim a clean-machine or end-to-end audio acceptance result.
"""
import sys

# A release rejects bytecode caches, so set this before importing package code.
sys.dont_write_bytecode = True

import argparse
import datetime
import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gpu_info():
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"status": "NOT_AVAILABLE", "detail": "nvidia-smi not found on PATH"}
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "ERROR", "detail": type(exc).__name__ + ": " + str(exc)}
    return {"status": "PASS" if result.returncode == 0 else "ERROR",
            "returncode": result.returncode, "stdout": result.stdout.strip()[:4000],
            "stderr": result.stderr.strip()[:1000]}


def _test_matrix():
    return {
        "clean_windows_10": "NOT_RUN",
        "clean_windows_11": "NOT_RUN",
        "nvidia_gtx": "NOT_RUN",
        "nvidia_rtx": "NOT_RUN",
        "cpu_only": "NOT_RUN",
        "audio_polish": "NOT_RUN",
        "audio_voice_conversion": "NOT_RUN",
        "audio_transcribe": "NOT_RUN",
        "resume_and_cache": "NOT_RUN",
        "cancel_and_recovery": "NOT_RUN",
        "tampered_output_detection": "NOT_RUN",
    }


def build_report(root, verification, operator_attested=False):
    manifest_path = root / "runtime-manifest" / "runtime.json"
    try:
        runtime_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        runtime_digest = runtime_manifest.get("snapshot_sha256")
    except (OSError, UnicodeError, json.JSONDecodeError):
        runtime_digest = None
    return {
        "schema_version": 1,
        "recorded_at_utc": datetime.datetime.now(datetime.timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z"),
        "profile": "portable-preflight",
        "classification": "OPERATOR_ATTESTED_PREFLIGHT" if operator_attested else "SIMULATED_PREFLIGHT",
        "operator_clean_machine_attestation": bool(operator_attested),
        "runtime_manifest_sha256": _sha256(manifest_path) if manifest_path.is_file() else None,
        "runtime_snapshot_sha256": runtime_digest,
        "runtime_verification": verification,
        "host": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "python_executable": str(Path(sys.executable).resolve()),
            "gpu_probe": _gpu_info(),
        },
        "acceptance_matrix": _test_matrix(),
        "public_distribution": "UNKNOWN_BLOCKED_PENDING_RIGHTS_REVIEW",
        "note": "Runtime preflight is not clean-machine, model-mode, or release acceptance.",
    }


def _output_path(root, data_root, requested):
    destination = Path(requested).expanduser() if requested else (
        data_root / "acceptance" /
        ("preflight-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"))
    if not destination.is_absolute():
        raise ValueError("--output must be an absolute path")
    destination = destination.resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("acceptance evidence must be outside the immutable Portable program tree")
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="absolute evidence JSON path; defaults under release user data")
    parser.add_argument("--operator-attests-clean-machine", action="store_true",
                        help="record operator attestation; this never changes NOT_RUN matrix entries")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from core.paths import release_data_root
    from core.runtime_verify import verify_runtime

    data_root = release_data_root()
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["NUMBA_CACHE_DIR"] = str(data_root / "cache" / "numba")
    verification = verify_runtime(root=root, data_root=data_root / "acceptance-probe",
                                  python_override=root / "tools/RVC/RVC-beta-v2-0618/runtime/python.exe",
                                  timeout=args.timeout)
    report = build_report(root, verification, args.operator_attests_clean_machine)
    destination = _output_path(root, data_root, args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("refusing to overwrite acceptance evidence: " + str(destination))
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    print(json.dumps({"status": verification.get("status", "FAIL"),
                      "classification": report["classification"],
                      "evidence": str(destination),
                      "acceptance_matrix": report["acceptance_matrix"]},
                     ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if verification.get("status") in ("PASS", "PASS_WITH_WARNINGS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
