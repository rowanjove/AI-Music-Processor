"""Build a local, hash-pinned ``.ampkg`` component package.

This command only reads the selected managed files and writes the requested
ZIP.  It does not install, download, sign automatically, or copy user voice
models.  Use ``--signing-key`` only when an operator has an explicitly
trusted private RSA key.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parser():
    parser = argparse.ArgumentParser(description="Build a local AMPKG component package")
    parser.add_argument("--root", required=True, help="current application root")
    parser.add_argument("--components", required=True,
                        help="comma-separated managed component IDs")
    parser.add_argument("--output", required=True, help="new .ampkg output path")
    parser.add_argument("--signing-key", help="optional PEM RSA private key")
    parser.add_argument("--package-id", help="stable package id (otherwise generated)")
    parser.add_argument("--version", help="package version (defaults to root VERSION)")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    root = Path(args.root).resolve()
    # Running the script directly from a checkout must resolve the checkout
    # before importing its core package; this does not modify sys.path in a
    # frozen release where the launcher has already configured it.
    workspace = Path(__file__).resolve().parents[1]
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))
    from core.component_package import build_component_package

    result = build_component_package(
        root,
        [item.strip() for item in args.components.split(",") if item.strip()],
        Path(args.output),
        signing_key=args.signing_key,
        package_id=args.package_id,
        version=args.version,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("AMPKG build failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

