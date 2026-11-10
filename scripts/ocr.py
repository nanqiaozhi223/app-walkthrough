#!/usr/bin/env python3
"""Offline OCR for screenshots with Apple Vision; prints recognised text and pixel boxes as JSON."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HELPER_SOURCE_NAME = "vision_ocr.swift"
CACHE_DIR = Path.home() / "Library" / "Caches" / "app-walkthrough" / "ocr"
HELPER_TIMEOUT = 60
DEFAULT_MIN_CONFIDENCE = 0.3


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def fail(message: str, exit_code: int = 1, detail: str | None = None) -> None:
    payload: dict = {"error": message}
    if detail is not None:
        payload["detail"] = detail
    emit(payload)
    sys.exit(exit_code)


def ensure_helper() -> Path:
    """Compile the Swift helper on demand; reuse the cached binary when the
    source is unchanged. The binary name embeds a short SHA-256 of the source
    so any edit triggers a fresh build. A build lock serializes concurrent
    cold starts so parallel processes never compile at the same time."""
    source_path = Path(__file__).resolve().parent / HELPER_SOURCE_NAME
    if not source_path.exists():
        fail("swift source not found: {}".format(source_path))

    source_bytes = source_path.read_bytes()
    digest = hashlib.sha256(source_bytes).hexdigest()[:12]
    binary_path = CACHE_DIR / "vision_ocr_{}".format(digest)

    if binary_path.exists() and os.access(binary_path, os.X_OK):
        return binary_path

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail("cannot create cache directory: {}".format(CACHE_DIR), detail=str(exc))

    lock_path = CACHE_DIR / ".build.lock"
    with open(lock_path, "w") as lock_file:
        # Blocking lock: concurrent callers wait here instead of launching
        # competing swiftc processes that can starve each other.
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        # Another process may have finished the build while we waited.
        if binary_path.exists() and os.access(binary_path, os.X_OK):
            return binary_path

        # Compile to a unique temporary name inside the cache directory, then
        # rename atomically so concurrent runs never see a half-written binary.
        fd, tmp_name = tempfile.mkstemp(prefix=".vision_ocr_build_", dir=str(CACHE_DIR))
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            completed = subprocess.run(
                ["xcrun", "swiftc", "-O", str(source_path), "-o", str(tmp_path)],
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                tail = (completed.stderr or "").strip()[-2000:]
                fail("failed to compile vision_ocr.swift", detail=tail)
            os.chmod(tmp_path, 0o755)
            os.replace(str(tmp_path), str(binary_path))
        except FileNotFoundError as exc:
            fail("xcrun/swiftc toolchain not available", detail=str(exc))
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    return binary_path


def run_helper(binary_path: Path, image_path: str) -> list:
    try:
        completed = subprocess.run(
            [str(binary_path), image_path],
            capture_output=True,
            text=True,
            timeout=HELPER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        fail("OCR helper timed out after {} seconds".format(HELPER_TIMEOUT))
    except OSError as exc:
        fail("failed to execute OCR helper", detail=str(exc))

    stdout = (completed.stdout or "").strip()
    try:
        parsed = json.loads(stdout) if stdout else []
    except json.JSONDecodeError as exc:
        fail("invalid JSON from OCR helper", detail=str(exc))

    if isinstance(parsed, dict) and "error" in parsed:
        fail(parsed["error"],
             detail=parsed.get("detail"))
    if not isinstance(parsed, list):
        fail("unexpected OCR helper output", detail=stdout[-500:])
    if completed.returncode != 0:
        fail("OCR helper exited with code {}".format(completed.returncode),
             detail=(completed.stderr or "").strip()[-2000:])
    return parsed


def enrich(items: list, min_confidence: float) -> list:
    kept = []
    for item in items:
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < min_confidence:
            continue
        x = float(item.get("x", 0))
        y = float(item.get("y", 0))
        w = float(item.get("width", 0))
        h = float(item.get("height", 0))
        enriched = dict(item)
        enriched["confidence"] = confidence
        enriched["center_x"] = round(x + w / 2.0, 1)
        enriched["center_y"] = round(y + h / 2.0, 1)
        kept.append(enriched)
    return kept


def rank_matches(items: list, query: str) -> list:
    needle = query.casefold()
    exact, contains = [], []
    for item in items:
        text = str(item.get("text", ""))
        folded = text.casefold()
        if folded == needle:
            exact.append(item)
        elif needle in folded:
            contains.append(item)
    return exact + contains


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="本地离线 OCR 工具（基于 Apple Vision 框架），识别截图中的文字并输出 JSON。",
        epilog=("注意：输出的坐标单位是像素（pixels）。"
                "若要换算为 iOS 逻辑分辨率的点（points），请将坐标除以屏幕缩放比例 "
                "（例如 iPhone @3x 截图需除以 3）。"),
    )
    parser.add_argument("image", help="待识别的截图路径（PNG/JPEG）")
    parser.add_argument("--query", metavar="TEXT", default=None,
                        help="只输出包含该文本的条目（大小写不敏感，精确匹配优先）")
    parser.add_argument("--min-confidence", metavar="FLOAT",
                        type=float, default=DEFAULT_MIN_CONFIDENCE,
                        help="最低置信度阈值，0 到 1 之间，默认 0.3")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    image_path = args.image

    if not Path(image_path).exists():
        fail("image not found: {}".format(image_path), exit_code=2)

    binary_path = ensure_helper()
    raw_items = run_helper(binary_path, image_path)
    items = enrich(raw_items, args.min_confidence)

    if args.query is None:
        emit({
            "image": image_path,
            "count": len(items),
            "items": items,
        })
    else:
        matches = rank_matches(items, args.query)
        emit({
            "image": image_path,
            "query": args.query,
            "count": len(matches),
            "matches": matches,
        })


if __name__ == "__main__":
    main()
