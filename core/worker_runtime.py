"""Small, dependency-free helpers shared by the optional GPU workers.

The parent process owns retry policy.  Worker processes only report a structured
error, write outputs through a temporary file/directory, and exit.  This is
deliberate: a CUDA context which has raised OOM is not a safe place to keep
running inference in the same process.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Iterable, Mapping, Sequence


MAX_ERROR_TAIL = 12_000


def is_cuda_oom(value: object) -> bool:
    """Return True only for errors which identify CUDA allocation failure."""

    text = str(value).lower()
    if "outofmemoryerror" in text and "cuda" in text:
        return True
    if "cuda out of memory" in text or "cuda error: out of memory" in text:
        return True
    if "cublas_status_alloc_failed" in text or "cudnn_status_alloc_failed" in text:
        return True
    if "out of memory" in text and any(token in text for token in ("cublas", "cudnn", "cuda", "gpu")):
        return True
    return False


def resolve_device(requested: str, torch_module) -> str:
    """Resolve ``auto`` and reject an unavailable explicit CUDA request."""

    requested = str(requested or "auto").lower()
    if requested not in {"auto", "cuda", "cpu"}:
        raise ValueError("device 必须是 auto/cuda/cpu")
    if requested == "cuda":
        if not torch_module.cuda.is_available():
            raise RuntimeError("requested CUDA device is unavailable")
        return "cuda"
    if requested == "auto":
        return "cuda" if torch_module.cuda.is_available() else "cpu"
    return "cpu"


def clear_cuda(torch_module) -> None:
    """Release unreferenced allocator blocks without hiding the original error."""

    try:
        if torch_module.cuda.is_available():
            torch_module.cuda.empty_cache()
            try:
                torch_module.cuda.ipc_collect()
            except Exception:
                pass
    except Exception:
        pass


def emit_worker_error(category: str, phase: str, device: str, error: BaseException, **extra) -> None:
    """Write a machine-readable error line while keeping the human tail short."""

    payload = {
        "worker_error": {
            "category": str(category),
            "phase": str(phase),
            "device": str(device),
            "retryable": str(category) == "cuda_oom",
            "error_type": type(error).__name__,
            "message": str(error)[-MAX_ERROR_TAIL:],
        }
    }
    payload["worker_error"].update(extra)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


def parse_worker_error(value: object) -> dict:
    """Read the last structured worker_error line before using text heuristics."""

    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    for line in reversed(text.splitlines()):
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        error = payload.get("worker_error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            return error
    return {}


def _write_sidecar(path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = path + f".part.{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def _replace_arg(command: Sequence[object], name: str, value: object) -> list[str]:
    """Replace a two-token CLI option, or append it when absent."""

    result: list[str] = []
    skip = False
    wanted = "--" + name.lstrip("-")
    for token in command:
        token = str(token)
        if skip:
            skip = False
            continue
        if token == wanted:
            skip = True
            continue
        result.append(token)
    result.extend([wanted, str(value)])
    return result


def build_worker_command(command: Sequence[object], overrides: Mapping[str, object] | None = None) -> list[str]:
    result = [str(part) for part in command]
    for name, value in (overrides or {}).items():
        result = _replace_arg(result, name, value)
    return result


def run_worker_with_recovery(
    command: Sequence[object],
    output_paths: Iterable[str],
    *,
    timeout: float,
    gpu_overrides: Mapping[str, object] | None = None,
    sidecar_path: str | None = None,
    run_fn=None,
):
    """Run a worker with a bounded GPU-reduction then fresh CPU retry.

    The first invocation lets the worker select its normal device.  Only a
    clearly identified CUDA OOM enables the two fallback attempts.  Each
    attempt is a new subprocess, so no damaged CUDA context is reused.  The
    returned ``CompletedProcess`` is from the successful attempt.
    """

    if run_fn is None:
        from core.processes import run as run_fn  # local import avoids cycles

    base = build_worker_command(command)
    reduced = dict(gpu_overrides or {})
    requested_device = "auto"
    for index, token in enumerate(base):
        if token == "--device" and index + 1 < len(base):
            requested_device = base[index + 1].lower()
            break
    if requested_device == "cpu":
        # An explicit CPU request is a user choice, not a request to probe GPU.
        attempts = [("cpu", {"device": "cpu"})]
    elif requested_device == "cuda":
        attempts = [
            ("gpu", {"device": "cuda"}),
            ("gpu-reduced", {**reduced, "device": "cuda"}),
        ]
    else:
        attempts = [
            ("auto", {"device": "auto"}),
            ("gpu-reduced", {"device": "cuda", **reduced}),
            ("cpu", {"device": "cpu"}),
        ]
    outputs = tuple(os.path.abspath(os.fspath(path)) for path in output_paths)
    failures: list[str] = []
    attempt_records: list[dict] = []
    deadline = time.monotonic() + float(timeout)
    for attempt_number, (label, overrides) in enumerate(attempts, 1):
        argv = build_worker_command(base, overrides)
        def identity(path):
            try:
                info = os.stat(path)
                return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
            except FileNotFoundError:
                return None
        before = {path:identity(path) for path in outputs}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failures.append("总超时预算已耗尽")
            break
        result = run_fn(argv, timeout=remaining, check=False)
        stdout = result.stdout or b""
        stderr = result.stderr or b""
        output_ok = result.returncode == 0 and all(os.path.isfile(path) and os.path.getsize(path)>0
                                                  and identity(path) != before[path] for path in outputs)
        structured = parse_worker_error(stderr) or parse_worker_error(stdout)
        category = str(structured.get("category", ""))
        if not structured and not output_ok and (is_cuda_oom(stderr) or is_cuda_oom(stdout)):
            category = 'cuda_oom'
        phase = str(structured.get("phase", "unknown"))
        selected_device = str(structured.get('device') or overrides.get("device", requested_device))
        if category == 'cuda_oom' and selected_device == 'auto': selected_device = 'cuda'
        terminal_worker_result = None
        for line in reversed(str(stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else stdout).splitlines()):
            try:
                status = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(status, dict) and status.get("worker") and status.get("device"):
                selected_device = str(status["device"])
                if status.get('status') == 'complete':
                    terminal_worker_result = status
                break
        error_tail = (stderr or stdout)
        if isinstance(error_tail, bytes):
            error_tail = error_tail.decode("utf-8", errors="replace")
        record = {
            "attempt": attempt_number,
            "label": label,
            "device": selected_device,
            "phase": phase if not output_ok else "complete",
            "category": category or ("success" if output_ok else "worker_error"),
            "returncode": result.returncode,
            "stderr_tail": str(error_tail)[-MAX_ERROR_TAIL:],
            "success": bool(output_ok),
        }
        attempt_records.append(record)
        if output_ok:
            metadata = {
                "status": "complete",
                "selected_device": record["device"],
                "recovery_attempts": attempt_records,
                "output_paths": list(outputs),
            }
            if terminal_worker_result is not None:
                metadata['terminal_worker_result'] = terminal_worker_result
            try:
                result.recovery_attempts = attempt_records
                result.worker_metadata = metadata
            except Exception:
                pass
            if attempt_number > 1:
                print(json.dumps({"worker_recovery": metadata}, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
            if sidecar_path:
                _write_sidecar(os.path.abspath(sidecar_path), metadata)
            return result
        combined = "\n".join(
            value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
            for value in (stdout, stderr)
        )
        if result.returncode == 0:
            failures.append(f"{label}: worker returned success but output is missing, empty or not generated by this attempt")
            break
        # A structured category is authoritative.  Text matching is retained
        # only for older workers which predate worker_error JSON.
        oom = category == "cuda_oom" if category else is_cuda_oom(combined)
        if not oom:
            failures.append(f"{label}: {combined[-MAX_ERROR_TAIL:]}")
            break
        if attempt_number < len(attempts):
            next_label = attempts[attempt_number][0]
            notice = {"worker_recovery": {"attempt": attempt_number, "from": label,
                                           "to": next_label, "category": "cuda_oom",
                                           "phase": phase}}
            print(json.dumps(notice, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
            action = "retrying with the next bounded configuration"
        else:
            action = "no recovery attempt remains"
        failures.append(f"{label}: CUDA OOM; {action}; stderr tail: {str(error_tail)[-MAX_ERROR_TAIL:]}")

    detail = "\n".join(failures)[-MAX_ERROR_TAIL:]
    raise RuntimeError("GPU worker failed after bounded CUDA recovery attempts:\n" + detail)
