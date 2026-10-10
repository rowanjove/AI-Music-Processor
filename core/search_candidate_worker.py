"""Bounded parameter-search adapter around the frozen RVC converter.

This script is launched as a subprocess by the parent search budget. Killing
the parent process tree on timeout also stops its nested RVC worker.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def run_request(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        request = json.load(handle)
    if not isinstance(request, dict):
        raise ValueError("搜索候选请求必须是对象")
    required = ("input", "output", "model_key", "pitch_shift", "f0_method", "index_rate", "profile")
    if any(key not in request for key in required):
        raise ValueError("搜索候选请求缺少冻结参数")
    from core.rvc_engine import convert_voice

    output = convert_voice(
        request["input"], request["output"], model_key=request["model_key"],
        pitch_shift=request["pitch_shift"], f0_method=request["f0_method"],
        index_rate=request["index_rate"], temp_dir=request.get("temp_dir"),
        profile=request["profile"],
    )
    if not os.path.isfile(output) or os.path.getsize(output) <= 0:
        raise RuntimeError("搜索候选未生成输出")
    return {"status": "complete", "output": os.path.abspath(output)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    try:
        report = run_request(args.request)
        print(json.dumps({"worker": "rvc_search_candidate", **report}, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"worker": "rvc_search_candidate", "status": "failed",
                          "error_type": type(exc).__name__, "error": str(exc)[-4000:]},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
