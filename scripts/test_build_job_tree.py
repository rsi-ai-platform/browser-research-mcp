#!/usr/bin/env python3
"""Debug helper for tools._build_job_tree.

Prints the exact `job_tree` structure (and wrapper payload) that would be
included in the external fallback response and passed along to the LLM/tool
consumer.

Examples:
  # Use built-in sample files
  python scripts/test_build_job_tree.py

  # Use real files list from JSON payload
  python scripts/test_build_job_tree.py --files-json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _ensure_src_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    src_str = str(src_dir)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def _load_tools_funcs():
    """Import `_build_job_tree` without requiring patchright at runtime.

    `browser_research.tools` imports patchright at module import time. For this
    local debug script we only need `_build_job_tree`, so we provide a minimal
    import fallback when patchright is unavailable.
    """
    try:
        from browser_research.tools import (  # type: ignore
            _build_file_tree_text,
            _job_rel_paths,
            fetch_kryptos_job_data,
        )

        return _job_rel_paths, _build_file_tree_text, fetch_kryptos_job_data
    except ModuleNotFoundError as exc:
        if exc.name != "patchright":
            raise

        import types

        fake_patchright = types.ModuleType("patchright")
        fake_async_api = types.ModuleType("patchright.async_api")
        fake_async_api.async_playwright = None
        fake_async_api.Browser = object
        fake_async_api.BrowserContext = object
        fake_async_api.Page = object

        sys.modules["patchright"] = fake_patchright
        sys.modules["patchright.async_api"] = fake_async_api
        from browser_research.tools import (  # type: ignore
            _build_file_tree_text,
            _job_rel_paths,
            fetch_kryptos_job_data,
        )

        return _job_rel_paths, _build_file_tree_text, fetch_kryptos_job_data


def _read_files_from_json(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        files = raw
    elif isinstance(raw, dict):
        if isinstance(raw.get("files"), list):
            files = raw["files"]
        elif isinstance(raw.get("gcs_result"), dict) and isinstance(raw["gcs_result"].get("files"), list):
            files = raw["gcs_result"]["files"]
        else:
            raise ValueError(
                "Could not find files list. Expected one of: top-level list, "
                "obj.files, or obj.gcs_result.files."
            )
    else:
        raise ValueError("JSON root must be an object or array.")

    normalized: list[dict[str, Any]] = []
    for item in files:
        if isinstance(item, dict):
            normalized.append(item)
    return normalized


def _sample_files(prefix: str) -> list[dict[str, Any]]:
    p = prefix.strip("/")
    return [
        {
            "name": f"{p}/article.html",
            "size": 8152,
            "content_type": "text/html",
            "updated": "2026-06-17T04:10:00+00:00",
        },
        {
            "name": f"{p}/data/summary.json",
            "size": 1290,
            "content_type": "application/json",
            "updated": "2026-06-17T04:10:04+00:00",
        },
        {
            "name": f"{p}/data/tables/table_1.csv",
            "size": 3402,
            "content_type": "text/csv",
            "updated": "2026-06-17T04:10:08+00:00",
        },
        {
            "name": f"{p}/screenshots/page.png",
            "size": 210_553,
            "content_type": "image/png",
            "updated": "2026-06-17T04:10:09+00:00",
        },
    ]


def _derive_prefix_root(prefix: str, job_name: str) -> str:
    p = prefix.strip("/")
    j = job_name.strip("/")
    suffix = f"/{j}"
    if p.endswith(suffix):
        return p[: -len(suffix)]
    return p


def _build_prefix(prefix_root: str, job_name: str) -> str:
    root = prefix_root.strip("/")
    job = job_name.strip("/")
    if not root:
        return f"{job}/"
    return f"{root}/{job}/"


async def _fetch_files_from_gcs(
    *,
    fetch_kryptos_job_data,
    job_name: str,
    bucket: str,
    prefix_root: str,
    wait_for_files: bool,
    wait_timeout_s: int,
    poll_interval_s: int,
    max_files: int,
) -> dict[str, Any]:
    return await fetch_kryptos_job_data(
        job_name,
        bucket=bucket,
        prefix_root=prefix_root,
        wait_for_files=wait_for_files,
        wait_timeout_s=wait_timeout_s,
        poll_interval_s=poll_interval_s,
        max_files=max_files,
        include_content=False,
        max_content_bytes=200_000,
    )


async def _fetch_files_with_prefix_candidates(
    *,
    fetch_kryptos_job_data,
    job_name: str,
    bucket: str,
    prefix_roots: list[str],
    wait_for_files: bool,
    wait_timeout_s: int,
    poll_interval_s: int,
    max_files: int,
) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None
    for root in prefix_roots:
        payload = await _fetch_files_from_gcs(
            fetch_kryptos_job_data=fetch_kryptos_job_data,
            job_name=job_name,
            bucket=bucket,
            prefix_root=root,
            wait_for_files=wait_for_files,
            wait_timeout_s=wait_timeout_s,
            poll_interval_s=poll_interval_s,
            max_files=max_files,
        )
        if isinstance(payload, dict) and payload.get("error"):
            last_payload = payload
            continue
        files = payload.get("files") if isinstance(payload, dict) else None
        if isinstance(files, list) and files:
            return payload
        last_payload = payload if isinstance(payload, dict) else None
    return last_payload or {
        "error": "GCS fetch failed for all prefix candidates.",
        "error_kind": "gcs_error",
        "job_name": job_name,
        "bucket": bucket,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test browser_research.tools._build_job_tree")
    parser.add_argument(
        "--job-name",
        required=True,
        help="Kryptos job name (required). Prefix is auto-built from this.",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Optional full prefix override (normally auto-derived).",
    )
    parser.add_argument(
        "--files-json",
        type=Path,
        default=None,
        help=(
            "Path to JSON containing file metadata. Accepts: list of files, "
            "{files:[...]}, or {gcs_result:{files:[...]}}."
        ),
    )
    parser.add_argument(
        "--from-gcs",
        action="store_true",
        help="Fetch real file metadata from GCS via fetch_kryptos_job_data().",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Use built-in sample file metadata (debug/demo only).",
    )
    parser.add_argument(
        "--bucket",
        default="single-url-data",
        help="GCS bucket for --from-gcs mode.",
    )
    parser.add_argument(
        "--prefix-root",
        default="data",
        help=(
            "Prefix root for --from-gcs mode (default: data). "
            "Final prefix is <prefix_root>/<job_name>/ unless --prefix is set."
        ),
    )
    parser.add_argument(
        "--cred-json",
        type=Path,
        default=None,
        help=(
            "Local path to GCP service-account JSON. If set, script exports "
            "GOOGLE_APPLICATION_CREDENTIALS for this process."
        ),
    )
    parser.add_argument(
        "--wait-for-files",
        action="store_true",
        help="When used with --from-gcs, poll until files appear or timeout.",
    )
    parser.add_argument(
        "--wait-timeout-s",
        type=int,
        default=60,
        help="Timeout seconds for --from-gcs polling.",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=int,
        default=5,
        help="Poll interval seconds for --from-gcs polling.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=50,
        help="Max files to read from GCS in --from-gcs mode.",
    )
    parser.add_argument(
        "--tree-only",
        action="store_true",
        help="Print only the job_tree object.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print compact one-line JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _ensure_src_on_path()
    job_rel_paths, build_file_tree_text, fetch_kryptos_job_data = _load_tools_funcs()

    selected_sources = int(args.files_json is not None) + int(args.from_gcs) + int(args.sample)
    if selected_sources > 1:
        print(
            "ERROR: Select at most one source: --from-gcs OR --files-json OR --sample",
            file=sys.stderr,
        )
        return 2

    source = "gcs"
    gcs_meta: dict[str, Any] | None = None
    resolved_prefix = (args.prefix or _build_prefix(args.prefix_root, args.job_name)).strip("/") + "/"
    resolved_bucket = args.bucket

    if args.cred_json is not None:
        cred_path = args.cred_json.expanduser().resolve()
        if not cred_path.exists():
            print(
                f"ERROR: cred json not found: {cred_path}",
                file=sys.stderr,
            )
            return 2
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cred_path)

    if args.files_json is not None:
        source = "files_json"
        files_meta = _read_files_from_json(args.files_json)
    elif args.from_gcs:
        source = "gcs"
        prefix_root = args.prefix_root
        if args.prefix is not None:
            prefix_root = _derive_prefix_root(args.prefix, args.job_name)
        gcs_payload = asyncio.run(
            _fetch_files_from_gcs(
                fetch_kryptos_job_data=fetch_kryptos_job_data,
                job_name=args.job_name,
                bucket=args.bucket,
                prefix_root=prefix_root,
                wait_for_files=bool(args.wait_for_files),
                wait_timeout_s=max(0, int(args.wait_timeout_s)),
                poll_interval_s=max(1, int(args.poll_interval_s)),
                max_files=max(1, int(args.max_files)),
            )
        )
        if isinstance(gcs_payload, dict) and gcs_payload.get("error"):
            print(json.dumps(gcs_payload, indent=2, ensure_ascii=False), file=sys.stderr)
            return 1
        files_meta = gcs_payload.get("files") if isinstance(gcs_payload, dict) else []
        files_meta = files_meta if isinstance(files_meta, list) else []
        if isinstance(gcs_payload, dict):
            resolved_prefix = str(gcs_payload.get("prefix") or resolved_prefix)
            resolved_bucket = str(gcs_payload.get("bucket") or resolved_bucket)
            gcs_meta = {
                "bucket": gcs_payload.get("bucket"),
                "prefix": gcs_payload.get("prefix"),
                "ready": gcs_payload.get("ready"),
                "waited_seconds": gcs_payload.get("waited_seconds"),
                "polls": gcs_payload.get("polls"),
            }
    else:
        if args.sample:
            source = "sample"
            files_meta = _sample_files(resolved_prefix)
        else:
            # Default mode: fetch from GCS even without --from-gcs.
            prefix_candidates = [args.prefix_root]
            if args.prefix_root.strip("/") == "data":
                prefix_candidates.append(f"{args.bucket}/data")
            if args.prefix_root.strip("/") == f"{args.bucket}/data":
                prefix_candidates.append("data")
            gcs_payload = asyncio.run(
                _fetch_files_with_prefix_candidates(
                    fetch_kryptos_job_data=fetch_kryptos_job_data,
                    job_name=args.job_name,
                    bucket=args.bucket,
                    prefix_roots=prefix_candidates,
                    wait_for_files=bool(args.wait_for_files),
                    wait_timeout_s=max(0, int(args.wait_timeout_s)),
                    poll_interval_s=max(1, int(args.poll_interval_s)),
                    max_files=max(1, int(args.max_files)),
                )
            )
            if isinstance(gcs_payload, dict) and gcs_payload.get("error"):
                print(json.dumps(gcs_payload, indent=2, ensure_ascii=False), file=sys.stderr)
                return 1
            files_meta = gcs_payload.get("files") if isinstance(gcs_payload, dict) else []
            files_meta = files_meta if isinstance(files_meta, list) else []
            if isinstance(gcs_payload, dict):
                resolved_prefix = str(gcs_payload.get("prefix") or resolved_prefix)
                resolved_bucket = str(gcs_payload.get("bucket") or resolved_bucket)
                gcs_meta = {
                    "bucket": gcs_payload.get("bucket"),
                    "prefix": gcs_payload.get("prefix"),
                    "ready": gcs_payload.get("ready"),
                    "waited_seconds": gcs_payload.get("waited_seconds"),
                    "polls": gcs_payload.get("polls"),
                }

    rel_files = job_rel_paths(files=files_meta, prefix=resolved_prefix)
    files_tree = build_file_tree_text(args.job_name, rel_files)

    payload: dict[str, Any]
    if args.tree_only:
        payload = {"files_tree": files_tree}
    else:
        status = "completed" if rel_files else "unknown"
        if gcs_meta is not None and gcs_meta.get("ready") is False:
            status = "pending"
        payload = {
            "kind": "external_fallback",
            "source": "kryptos_single_url",
            "url": None,
            "job_name": args.job_name,
            "job_id": None,
            "status": status,
            "file_count": len(rel_files),
            "files": rel_files,
            "files_tree": files_tree,
            "next_step": "Call rescue_fetch(job_name=...) to read file contents from this job tree.",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    if args.compact:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
