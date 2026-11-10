#!/usr/bin/env python3
"""Drive a USB-connected iPhone through Appium/XCUITest, one logged action at a time.

Every command works on a run directory (see `start-run`): screenshots and their
metadata go to evidence/, every action is appended to events.jsonl, and report.py
later turns that directory into a report.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE.parent / "runs"
APPIUM_SERVER = os.environ.get("APPIUM_SERVER", "http://127.0.0.1:4723/wd/hub").rstrip("/")


def now() -> str:
    """UTC timestamp, second precision, with a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_command(command: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return result.returncode, (result.stdout or result.stderr).strip()


def appium_binary() -> str | None:
    candidates = [
        os.environ.get("APPIUM_BIN", ""),
        shutil.which("appium") or "",
        str(Path.home() / ".npm-global" / "bin" / "appium"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def appium_request(server: str, method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 120) -> dict[str, Any]:
    url = f"{server.rstrip('/')}/{path.lstrip('/')}"
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method.upper(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
            # Keep the Appium message; the Node stack trace is in the Appium log.
            detail = parsed.get("value", {}).get("message") or parsed
        except (json.JSONDecodeError, AttributeError):
            detail = raw[:800]
        raise SystemExit(f"Appium 请求失败 {method} {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"无法连接 Appium：{url}\n{exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Appium 返回了非 JSON 响应：{raw[:500]}") from exc


def probe_appium(server: str) -> bool:
    try:
        response = appium_request(server, "GET", "/status", timeout=5)
    except SystemExit:
        return False
    return isinstance(response.get("value"), dict)


def ensure_appium(server: str, start: bool = True) -> None:
    if probe_appium(server):
        return
    if not start:
        raise SystemExit(f"Appium 未运行：{server}。请先启动 Appium，或使用 --start-appium。")
    binary = appium_binary()
    if not binary:
        raise SystemExit("找不到 appium。请安装 Appium 3 和 xcuitest 驱动，或设置 APPIUM_BIN。")
    parsed = re.match(r"https?://([^:/]+)(?::(\d+))?(?:/.*)?$", server)
    host = parsed.group(1) if parsed else "127.0.0.1"
    port = parsed.group(2) if parsed and parsed.group(2) else "4723"
    log_path = Path(os.environ.get("APPIUM_LOG", "/tmp/appium-app-walkthrough.log"))
    log_handle = log_path.open("ab")
    subprocess.Popen(
        [binary, "--address", host, "--port", port, "--base-path", "/wd/hub"],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(30):
        if probe_appium(server):
            return
        time.sleep(0.5)
    raise SystemExit(f"Appium 启动超时，请查看日志：{log_path}")


def list_iphones() -> list[dict[str, Any]]:
    """Return physical iPhones known to CoreDevice, parsed from devicectl's JSON output."""
    with tempfile.TemporaryDirectory(prefix="devicectl-") as temp:
        output = Path(temp) / "devices.json"
        code, _ = run_command(["xcrun", "devicectl", "list", "devices", "--json-output", str(output)], timeout=30)
        if code or not output.exists():
            return []
        try:
            data = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    phones: list[dict[str, Any]] = []
    for item in data.get("result", {}).get("devices", []):
        hardware = item.get("hardwareProperties", {})
        props = item.get("deviceProperties", {})
        connection = item.get("connectionProperties", {})
        if hardware.get("deviceType") != "iPhone" or hardware.get("reality") != "physical":
            continue
        phones.append({
            "udid": hardware.get("udid"),
            "name": props.get("name"),
            "model": hardware.get("marketingName"),
            "os_version": props.get("osVersionNumber"),
            "developer_mode": props.get("developerModeStatus"),
            "pairing": connection.get("pairingState"),
            "transport": connection.get("transportType"),
            "connected": connection.get("tunnelState") == "connected" or bool(connection.get("transportType")),
        })
    return phones


def connected_udid(requested: str | None) -> str:
    if requested:
        return requested
    env_udid = os.environ.get("IOS_UDID")
    if env_udid:
        return env_udid
    phones = [phone for phone in list_iphones() if phone["connected"] and phone["udid"]]
    if not phones:
        raise SystemExit("没有找到已连接的 iPhone。请解锁设备、用 USB 连接，并确认已在 Xcode 配对。")
    if len(phones) > 1:
        listing = ", ".join(f"{phone['name']}={phone['udid']}" for phone in phones)
        raise SystemExit(f"检测到多台 iPhone，请用 --udid 指定：{listing}")
    phone = phones[0]
    if phone.get("developer_mode") not in (None, "enabled"):
        raise SystemExit(f"{phone['name']} 未开启开发者模式：设置 → 隐私与安全性 → 开发者模式。")
    return str(phone["udid"])


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def require_run(path: str) -> Path:
    """Resolve RUN_DIR and refuse directories that start-run did not create."""
    run = Path(path).expanduser().resolve()
    if not run.joinpath("manifest.json").is_file():
        raise SystemExit(f"{run} 不是 start-run 创建的目录（缺少 manifest.json）")
    run.joinpath("evidence").mkdir(exist_ok=True)
    return run


def append_event(run: Path, event: dict[str, Any]) -> None:
    """Add one line to the run log; report.py rebuilds the journey from it."""
    line = json.dumps({"timestamp": now(), **event}, ensure_ascii=False)
    with open(run / "events.jsonl", "a", encoding="utf-8") as log:
        log.write(line + "\n")


def event_count(run: Path) -> int:
    log = run / "events.jsonl"
    if not log.is_file():
        return 0
    # Iterating the file splits on newlines only, so a U+2028 inside a post stays in its row.
    with open(log, encoding="utf-8") as handle:
        return sum(1 for row in handle if row.strip())

def session_info(run: Path) -> dict[str, Any]:
    path = run / "session.json"
    if not path.exists():
        raise SystemExit(f"run 没有 Appium session：{run}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"session.json 损坏：{path}") from exc


def session_request(run: Path, method: str, suffix: str = "", payload: dict[str, Any] | None = None, timeout: int = 120) -> dict[str, Any]:
    info = session_info(run)
    server = str(info["server"])
    sid = str(info["session_id"])
    path = f"/session/{sid}{suffix}"
    return appium_request(server, method, path, payload, timeout=timeout)


def png_size(data: bytes) -> tuple[int | None, int | None]:
    """Width and height from the PNG header, without decoding any pixels."""
    if len(data) >= 24 and data.startswith(b"\x89PNG") and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None, None


# Scroll checks compare small grayscale thumbnails of two screenshots. The status bar
# (clock, battery) and the bottom strip (home indicator, tab bar) change on their own
# or not at all, so a band at each end is left out of the comparison.
THUMB_WIDTH, THUMB_HEIGHT = 36, 80
SKIP_TOP = SKIP_BOTTOM = 0.08


def thumbnail(image: Path) -> list[int]:
    """Gray levels of the content area of `image`, scaled down with macOS `sips`."""
    with tempfile.TemporaryDirectory(prefix="walkthrough-thumb-") as temp:
        bmp = Path(temp) / "thumb.bmp"
        code, output = run_command(
            ["sips", "-s", "format", "bmp", "-z", str(THUMB_HEIGHT), str(THUMB_WIDTH), str(image), "--out", str(bmp)],
            timeout=30,
        )
        if code or not bmp.is_file():
            raise ValueError(f"sips 无法转换截图：{output[-200:]}")
        data = bmp.read_bytes()
    pixels_at = int.from_bytes(data[10:14], "little")
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = int.from_bytes(data[22:26], "little", signed=True)
    depth = int.from_bytes(data[28:30], "little")
    if depth not in (24, 32) or width <= 0 or height == 0:
        raise ValueError(f"缩略图格式不支持：{depth} bit，{width}x{height}")
    step = depth // 8
    stride = (width * step + 3) & ~3  # BMP rows are padded to 4 bytes
    rows = []
    for row in range(abs(height)):
        start = pixels_at + row * stride
        rows.append([sum(data[start + col * step:start + col * step + 3]) // 3 for col in range(width)])
    if height > 0:  # a positive height means the rows are stored bottom-up
        rows.reverse()
    keep = rows[round(len(rows) * SKIP_TOP):len(rows) - round(len(rows) * SKIP_BOTTOM)]
    return [level for row in keep for level in row]


def screen_change(before: Path, after: Path) -> float:
    """How different two screenshots look: 0 means identical, 1 means black versus white."""
    first, second = thumbnail(before), thumbnail(after)
    if len(first) != len(second):
        return 1.0
    if not first:
        return 0.0
    return sum(abs(a - b) for a, b in zip(first, second)) / (255 * len(first))


def evidence_number(path: Path) -> int:
    found = re.match(r"evidence-(\d+)-", path.name)
    return int(found.group(1)) if found else 0


def latest_screenshot(run: Path) -> Path | None:
    """Newest evidence PNG still on disk; scroll checks delete the shots they discard."""
    shots = sorted(run.joinpath("evidence").glob("evidence-*.png"), key=lambda path: (evidence_number(path), path.name))
    return shots[-1] if shots else None


def capture(run: Path, title: str, reason: str, save_ui: bool = False) -> Path:
    """Save the current screen (and optionally its UI tree) as the next piece of evidence."""
    encoded = session_request(run, "GET", "/screenshot", timeout=60).get("value")
    if not isinstance(encoded, str):
        raise SystemExit("Appium 没有返回截图数据")
    png = base64.b64decode(encoded)
    step = event_count(run) + 1
    stem = f"evidence-{step:03d}-{datetime.now():%Y%m%d-%H%M%S}"
    folder = run / "evidence"
    folder.joinpath(f"{stem}.png").write_bytes(png)
    tree_file = None
    if save_ui:
        tree = session_request(run, "GET", "/source", timeout=60).get("value", "")
        tree_file = f"{stem}.xml"
        folder.joinpath(tree_file).write_text(str(tree), encoding="utf-8")
    width, height = png_size(png)
    record = {
        "step": step, "title": title, "reason": reason, "image": f"{stem}.png", "ui_dump": tree_file,
        "image_width": width, "image_height": height, "timestamp": now(),
    }
    write_json(folder / f"{stem}.json", record)
    append_event(run, {"kind": "evidence", **record})
    print(folder / f"{stem}.png")
    return folder / f"{stem}.png"


def execute_script(run: Path, script: str, args: list[Any] | None = None) -> dict[str, Any]:
    return session_request(run, "POST", "/execute/sync", {"script": script, "args": args or []}, timeout=120)


# Labels that usually mean an external side effect. tap-text refuses them unless
# the user has explicitly approved the action and --allow-risky is passed.
RISKY_WORDS = (
    "购买", "支付", "付款", "下单", "提交订单", "删除", "发布", "发送", "订阅", "开通",
    "续费", "领取", "激活", "注销", "退出登录", "pay", "buy", "purchase", "delete", "send", "subscribe",
)
# Icon asset names, dotted class names, UIKit-internal PascalCase views, scrollbar labels.
NOISE_TEXT = re.compile(r"(?i:^(ic[ _-]|img_|search_|live_tag))|^[A-Za-z]+(\.[A-Za-z]+)+$|^(?:[A-Z][a-z]+){3,}$|^垂直滚动条|^\d+%$")


def page_source(run: Path) -> ET.Element:
    source = session_request(run, "GET", "/source", timeout=60).get("value", "")
    if not isinstance(source, str) or not source.strip():
        raise SystemExit("Appium 没有返回页面结构")
    try:
        return ET.fromstring(source)
    except ET.ParseError as exc:
        raise SystemExit(f"无法解析页面结构：{exc}") from exc


def page_texts(root: ET.Element, include_hidden: bool = False, dedupe: bool = True) -> list[dict[str, Any]]:
    """Collect readable text with point-based bounds from an XCUITest source tree."""
    items: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for element in root.iter():
        attrs = element.attrib
        if not include_hidden and attrs.get("visible") != "true":
            continue
        text = (attrs.get("label") or attrs.get("value") or attrs.get("name") or "").strip()
        if not text or NOISE_TEXT.search(text):
            continue
        try:
            x, y, width, height = (int(float(attrs.get(key, "0"))) for key in ("x", "y", "width", "height"))
        except ValueError:
            x = y = width = height = 0
        key = text if dedupe else (text, x, y, width, height)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "text": text,
            "type": element.tag.replace("XCUIElementType", ""),
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        })
    return items


def contains_box(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    return (
        outer["x"] <= inner["x"]
        and outer["y"] <= inner["y"]
        and outer["x"] + outer["width"] >= inner["x"] + inner["width"]
        and outer["y"] + outer["height"] >= inner["y"] + inner["height"]
    )


def innermost(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop container elements that merely wrap another match with the same label."""
    return [
        item for item in matches
        if not any(other is not item and contains_box(item, other) for other in matches)
    ]


def judge_scroll(run: Path, before: Path, after: Path, threshold: float) -> None:
    """Keep `after` only if the page visibly moved; print and log the verdict as JSON."""
    try:
        change = screen_change(before, after)
    except (OSError, ValueError) as exc:
        verdict: dict[str, Any] = {"changed": None, "image": str(after), "message": "无法自动比较，请人工确认截图"}
        append_event(run, {"kind": "scroll_check", "changed": None, "comparison_error": str(exc), "image": after.name})
        print(json.dumps(verdict, ensure_ascii=False))
        return
    moved = change > threshold
    if not moved:
        # Same page as before: drop the shot so it does not become a duplicate figure.
        for suffix in (".png", ".json", ".xml"):
            after.with_suffix(suffix).unlink(missing_ok=True)
    append_event(run, {
        "kind": "scroll_check", "changed": moved, "difference": round(change, 6), "threshold": threshold,
        "discarded_image": None if moved else after.name,
    })
    print(json.dumps({
        "changed": moved, "difference": round(change, 6), "threshold": threshold,
        "image": str(after) if moved else None,
    }, ensure_ascii=False))


DATE_TEXT = re.compile(
    # Only a date, optionally followed by a time; “今天运营商打电话…” is content, not a date.
    r"^(刚刚|刚才|\d+\s*(秒|分钟|小时|天|周|个月|年)前|(今天|昨天|前天)(\s*\d{1,2}:\d{2})?"
    r"|\d{1,2}月\d{1,2}日(\s*\d{1,2}:\d{2})?|\d{4}[-./年]\d{1,2}[-./月]\d{1,2}日?(\s*\d{1,2}:\d{2})?"
    r"|\d{1,2}[-/]\d{1,2}|\d{1,2}:\d{2})$"
)
COUNT_TEXT = re.compile(r"^\d+(\.\d+)?[万wWkK]?\+?$")
AD_TEXT = re.compile(r"^(广告|下载安装|赞助|推广)$")
BACK_TEXT = re.compile(r"(?i)^(navigation[ _-]?back.*|back|返回|nav[ _-]?back.*|ic[ _-]?back.*)$")
COMMENT_LABEL = re.compile(r"^(楼主|作者|置顶|热评|神评|精选|.{0,6}回复了|Lv\d+|LV\d+)$")
REPLY_TOTAL = re.compile(r"(?:共|查看全部|展开)\s*(\d+)\s*条")


def frame(element: ET.Element) -> dict[str, int] | None:
    try:
        return {key: int(float(element.get(key, ""))) for key in ("x", "y", "width", "height")}
    except ValueError:
        return None


def element_text(element: ET.Element) -> str:
    text = element.get("label") or element.get("value") or element.get("name") or ""
    # Apps sometimes embed U+2028/U+2029; normalise them so posts read as ordinary lines.
    return text.replace("\u2028", "\n").replace("\u2029", "\n").strip()


def post_key(body: str) -> str:
    return re.sub(r"\s+", "", body)[:120]


def parse_posts(root: ET.Element, min_body: int = 20) -> list[dict[str, Any]]:
    """Split a feed into posts, one per innermost XCUIElementTypeCell.

    Field roles are heuristics: the first long text is the body, the first short
    text before it is the publisher, a date-like text is the date, numeric labels
    are interaction counts, and remaining short texts are labels (device, topic).
    """
    posts: list[dict[str, Any]] = []
    for cell in root.iter("XCUIElementTypeCell"):
        if any(inner is not cell for inner in cell.iter("XCUIElementTypeCell")):
            continue
        texts: list[tuple[str, str, ET.Element]] = []
        for element in cell.iter():
            text = element_text(element)
            if element is cell or not text or NOISE_TEXT.search(text):
                continue
            texts.append((element.tag.replace("XCUIElementType", ""), text, element))
        if any(AD_TEXT.match(text) for _, text, _ in texts):
            continue  # sponsored card, not a post
        body_index = next((n for n, (_, text, _) in enumerate(texts) if len(text) >= min_body), None)
        if body_index is None:
            continue
        body = texts[body_index][1]
        # A numeric Button usually wraps a StaticText with the same number; count it once.
        button_counts = [text for tag, text, _ in texts if tag == "Button" and COUNT_TEXT.match(text)]
        counts = button_counts or [text for _, text, _ in texts if COUNT_TEXT.match(text)]
        author = date = None
        labels: list[str] = []
        extra: list[str] = []
        for index, (_, text, _) in enumerate(texts):
            if index == body_index or COUNT_TEXT.match(text):
                continue
            if len(text) >= min_body:
                if text != body and text not in extra:
                    extra.append(text)
            elif date is None and DATE_TEXT.match(text):
                date = text
            elif author is None and index < body_index:
                author = text
            elif text not in labels and text != author:
                labels.append(text)
        posts.append({
            "author": author,
            "date": date,
            "body": body,
            "labels": labels,
            "counts": counts,
            "extra": extra[:3],
            "_frame": frame(texts[body_index][2]),  # where to tap to open the post; not saved
        })
    if posts or next(root.iter("XCUIElementTypeCell"), None) is not None:
        return posts
    # No cell structure at all (WebView or custom list): fall back to long visible texts.
    return [
        {"author": None, "date": None, "body": item["text"], "labels": [], "counts": [], "extra": [], "_frame": None}
        for item in page_texts(root)
        if len(item["text"]) >= min_body
    ]


def parse_comments(root: ET.Element, post_body: str) -> list[dict[str, Any]]:
    """Comments on a post detail page, one per innermost cell that has a date.

    Expected order inside a cell: commenter, comment text, (labels), date, like count,
    then reply previews such as “名字：内容”. Cells holding the post itself, sort tabs
    and sponsored cards are skipped.
    """
    marker = post_key(post_body)[:16]
    comments: list[dict[str, Any]] = []
    for cell in root.iter("XCUIElementTypeCell"):
        if any(inner is not cell for inner in cell.iter("XCUIElementTypeCell")):
            continue
        texts: list[tuple[str, str]] = []
        for element in cell.iter():
            text = element_text(element)
            if element is cell or not text or NOISE_TEXT.search(text):
                continue
            if texts and texts[-1][1] == text:
                continue  # Button + inner StaticText with the same label
            texts.append((element.tag.replace("XCUIElementType", ""), text))
        if not texts or any(AD_TEXT.match(text) for _, text in texts):
            continue
        if marker and any(marker in post_key(text) for _, text in texts):
            continue  # the post itself
        date_index = next((n for n, (_, text) in enumerate(texts) if DATE_TEXT.match(text)), None)
        if date_index is None or date_index < 2:
            continue  # needs at least commenter + text before the date
        before = texts[:date_index]
        author = before[0][1]
        labels = [text for _, text in before[1:] if COMMENT_LABEL.match(text)]
        bodies = [text for _, text in before[1:] if not COMMENT_LABEL.match(text) and not COUNT_TEXT.match(text)]
        if not bodies:
            continue
        after = texts[date_index + 1:]
        likes = next((text for tag, text in after if COUNT_TEXT.match(text)), None)
        replies: list[str] = []
        reply_total = None
        for _, text in after:
            total = REPLY_TOTAL.search(text)
            if total:
                reply_total = int(total.group(1))
            elif re.match(r"^[^：:]{1,24}[：:]\s*\S", text) and text not in replies:
                replies.append(text)
            elif COMMENT_LABEL.match(text) and text not in labels:
                labels.append(text)
        comments.append({
            "author": author,
            "date": texts[date_index][1],
            "body": "\n".join(bodies),
            "likes": likes,
            "labels": labels,
            "replies": replies[:3],
            "reply_total": reply_total if reply_total is not None else (len(replies) or None),
        })
    return comments


def back_button(root: ET.Element) -> tuple[int, int] | None:
    """Centre of a visible top-left back button, if the page has one."""
    for element in root.iter():
        if element.tag not in ("XCUIElementTypeButton", "XCUIElementTypeImage", "XCUIElementTypeOther"):
            continue
        box = frame(element)
        if not box or element.get("visible") == "false" or box["y"] > 130 or box["x"] > 120:
            continue
        if BACK_TEXT.match(element_text(element)):
            return box["x"] + box["width"] // 2, box["y"] + box["height"] // 2
    return None


def page_signature(root: ET.Element) -> str:
    return "|".join(item["text"] for item in page_texts(root))


# Controls that exist only on a post's own page, e.g. 酷安 "bottomBar_writeComment".
COMPOSER_TEXT = re.compile(r"(?i)(write[ _-]?comment|bottomBar_comment|^写评论|^说点什么|^发表评论|^添加评论|^评论一下)")


def is_post_detail(root: ET.Element, post_body: str) -> bool:
    """True when the page is the tapped post: its text is visible, or a comment box is present.

    Some apps draw the post body without accessible text, so the comment composer is the
    fallback signal; topic pages and image viewers do not have one.
    """
    marker = post_key(post_body)[:16]
    if marker and marker in post_key(page_signature(root)):
        return True
    return any(COMPOSER_TEXT.search(element_text(element)) for element in root.iter())


def merge_post(existing: dict[str, Any], post: dict[str, Any]) -> None:
    """Fill fields that were cut off when the same post was first seen at a screen edge."""
    for field in ("author", "date"):
        if not existing.get(field) and post.get(field):
            existing[field] = post[field]
    for field in ("labels", "counts", "extra"):
        if len(post.get(field) or []) > len(existing.get(field) or []):
            existing[field] = post[field]
    if len(post["body"]) > len(existing["body"]):
        existing["body"] = post["body"]


def saved(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def installed_apps(udid: str) -> list[dict[str, Any]] | None:
    """All apps on the phone, or None when devicectl cannot list them."""
    with tempfile.TemporaryDirectory(prefix="devicectl-apps-") as temp:
        output = Path(temp) / "apps.json"
        code, _ = run_command([
            "xcrun", "devicectl", "device", "info", "apps", "--device", udid,
            "--include-all-apps", "--json-output", str(output),
        ], timeout=60)
        if code or not output.exists():
            return None
        try:
            return json.loads(output.read_text(encoding="utf-8")).get("result", {}).get("apps", [])
        except json.JSONDecodeError:
            return None


def app_info(apps: list[dict[str, Any]] | None, bundle_id: str) -> dict[str, Any] | None:
    """Name/version for an installed app, {} if unknown, None if the list confirms it is missing."""
    if not apps:
        return {}
    for app in apps:
        if app.get("bundleIdentifier") == bundle_id:
            return {"name": app.get("name"), "version": app.get("version"), "build": app.get("bundleVersion")}
    return None


def detect_wda_bundle_id(apps: list[dict[str, Any]] | None) -> str | None:
    """The user's signed WDA Runner, e.g. com.name.WebDriverAgentRunner(.xctrunner)."""
    found = sorted({
        re.sub(r"\.xctrunner$", "", str(app["bundleIdentifier"]))
        for app in apps or []
        if "WebDriverAgentRunner" in str(app.get("bundleIdentifier", ""))
        and not str(app.get("bundleIdentifier")).startswith("com.facebook.")
    })
    return found[0] if len(found) == 1 else None


def detect_team_id() -> str | None:
    """Team ID from the single 'Apple Development' certificate in the login keychain."""
    code, pem = run_command(["security", "find-certificate", "-a", "-c", "Apple Development", "-p"], timeout=20)
    if code or "BEGIN CERTIFICATE" not in pem:
        return None
    teams = set()
    for cert in re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S):
        try:
            subject = subprocess.run(["openssl", "x509", "-noout", "-subject"], input=cert, capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        match = re.search(r"OU\s*=\s*([A-Z0-9]{10})\b", subject)
        if match:
            teams.add(match.group(1))
    return teams.pop() if len(teams) == 1 else None


def cmd_status(_: argparse.Namespace) -> None:
    rows = list_iphones()
    appium_ok = probe_appium(APPIUM_SERVER)
    print(json.dumps({"platform": "ios", "appium": appium_ok, "devices": rows}, ensure_ascii=False, indent=2))


def window_size(run: Path) -> tuple[int, int]:
    """Logical screen size in points; tap/swipe coordinates use this space."""
    rect = session_request(run, "GET", "/window/rect").get("value", {})
    width, height = int(rect.get("width", 0)), int(rect.get("height", 0))
    if not width or not height:
        raise SystemExit("无法读取 iPhone 屏幕尺寸")
    return width, height


def cmd_screen_size(args: argparse.Namespace) -> None:
    """Points (for taps) versus pixels (in screenshots), and the factor between them."""
    run = require_run(args.run)
    points_w, points_h = window_size(run)
    answer: dict[str, Any] = {"points": {"width": points_w, "height": points_h}}
    shot = latest_screenshot(run)
    pixels_w, pixels_h = png_size(shot.read_bytes()) if shot else (None, None)
    if pixels_w and pixels_h:
        answer["screenshot_pixels"] = {"width": pixels_w, "height": pixels_h}
        answer["scale"] = round(pixels_w / points_w, 3)
    print(json.dumps(answer, ensure_ascii=False))


def cmd_start_run(args: argparse.Namespace) -> None:
    if not args.bundle_id:
        raise SystemExit("必须指定目标 App 的 Bundle ID，例如 com.coolapk.app。")
    server = args.appium_server.rstrip("/")
    ensure_appium(server, start=args.start_appium)
    udid = connected_udid(args.udid)
    apps = installed_apps(udid)
    app = app_info(apps, args.bundle_id)
    if app is None:
        raise SystemExit(f"这台 iPhone 上没有安装 {args.bundle_id}。请先在 App Store 安装，或确认 Bundle ID。")
    phone = next((item for item in list_iphones() if item.get("udid") == udid), {})

    # Signing: explicit flag > environment variable > auto-detected from this Mac and phone.
    team_id, team_source = args.team_id, "--team-id"
    if not team_id and os.environ.get("IOS_TEAM_ID"):
        team_id, team_source = os.environ["IOS_TEAM_ID"], "IOS_TEAM_ID"
    if not team_id:
        team_id, team_source = detect_team_id(), "自动识别（钥匙串里的 Apple Development 证书）"
    wda_bundle_id, wda_source = args.wda_bundle_id, "--wda-bundle-id"
    if not wda_bundle_id and os.environ.get("IOS_WDA_BUNDLE_ID"):
        wda_bundle_id, wda_source = os.environ["IOS_WDA_BUNDLE_ID"], "IOS_WDA_BUNDLE_ID"
    if not wda_bundle_id:
        wda_bundle_id, wda_source = detect_wda_bundle_id(apps), "自动识别（手机上已安装的 WDA）"
    print(f"签名 Team：{team_id or '未找到'}（{team_source}）", file=sys.stderr)
    print(f"WDA Bundle ID：{wda_bundle_id or '未找到'}（{wda_source}）", file=sys.stderr)

    capabilities: dict[str, Any] = {
        "platformName": "iOS",
        "appium:automationName": "XCUITest",
        "appium:udid": udid,
        "appium:bundleId": args.bundle_id,
        "appium:noReset": True,
        "appium:newCommandTimeout": 3600,
        "appium:wdaLaunchTimeout": 120000,
        "appium:wdaConnectionTimeout": 120000,
        "appium:xcodeSigningId": args.signing_id or os.environ.get("IOS_SIGNING_ID", "Apple Development"),
    }
    if team_id:
        capabilities["appium:xcodeOrgId"] = team_id
    if wda_bundle_id:
        capabilities["appium:updatedWDABundleId"] = wda_bundle_id
    try:
        response = appium_request(server, "POST", "/session", {"capabilities": {"alwaysMatch": capabilities, "firstMatch": [{}]}}, timeout=300)
    except SystemExit as exc:
        message = str(exc)
        if "code 65" in message:
            hints = ["WebDriverAgent 编译或签名失败（xcodebuild code 65）。常见原因："]
            if not team_id:
                hints.append("- 没有找到签名 Team：用 --team-id 或 IOS_TEAM_ID 指定（Xcode → Settings → Accounts 里的 10 位 ID）")
            if not wda_bundle_id:
                hints.append("- 没有找到 WDA Bundle ID：用 --wda-bundle-id 或 IOS_WDA_BUNDLE_ID 指定")
            hints.append("- 免费 Personal Team 的签名约 7 天过期：在 Xcode 里对 WebDriverAgentRunner 重新执行 Product → Test")
            hints.append("- iPhone 锁屏或未信任开发者：解锁手机，到 设置 → 通用 → VPN 与设备管理 里信任")
            raise SystemExit("\n".join(hints)) from exc
        raise
    value = response.get("value", {})
    sid = response.get("sessionId") or value.get("sessionId")
    if not sid:
        raise SystemExit(f"Appium 创建 session 失败：{response}")

    # Only now, with a live session, does the run get a directory: a failed start leaves nothing.
    run = new_run_dir(args.title)
    write_json(run / "manifest.json", {
        "title": args.title, "task": args.task, "platform": "ios",
        "bundle_id": args.bundle_id, "udid": udid, "app": app,
        "device": {"model": phone.get("model"), "os_version": phone.get("os_version")},
        "started_at": now(), "status": "running",
    })
    write_json(run / "session.json", {"server": server, "session_id": sid, "capabilities": capabilities})
    append_event(run, {"kind": "run_started", "title": args.title, "task": args.task, "platform": "ios", "bundle_id": args.bundle_id})
    print(run)


def new_run_dir(title: str) -> Path:
    """runs/<time>-<ascii words of the title>; Chinese-only titles become 'walkthrough'."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{'-'.join(words)[:48] or 'walkthrough'}"
    run = RUNS_DIR / name
    run.joinpath("evidence").mkdir(parents=True)
    return run


def cmd_screenshot(args: argparse.Namespace) -> None:
    capture(require_run(args.run), args.title, args.reason, args.with_ui)


def cmd_dump_ui(args: argparse.Namespace) -> None:
    """Save the full XCUITest page source, overwriting the previous dump."""
    run = require_run(args.run)
    target = run / "ui-latest.xml"
    target.write_text(str(session_request(run, "GET", "/source", timeout=60).get("value", "")), encoding="utf-8")
    append_event(run, {"kind": "ui_dump", "path": target.name})
    print(target)


def log_action(args: argparse.Namespace, action: dict[str, Any]) -> None:
    """Record a finished action in the run log and echo it to the caller."""
    append_event(require_run(args.run), {"kind": "action", **action})
    print(json.dumps(action, ensure_ascii=False))


def target_bundle(run: Path) -> str:
    return json.loads((run / "manifest.json").read_text(encoding="utf-8"))["bundle_id"]


def foreground_bundle(run: Path) -> str | None:
    try:
        return execute_script(run, "mobile: activeAppInfo").get("value", {}).get("bundleId")
    except SystemExit:
        return None


def require_target_app(run: Path) -> None:
    """Refuse to act while another app is in front, e.g. after a tap followed a link into 闲鱼."""
    expected, current = target_bundle(run), foreground_bundle(run)
    if current and current != expected:
        raise SystemExit(
            f"当前前台是 {current}，不是目标 App {expected}，不会在其他 App 里操作。"
            "请先截图确认；如果是点到外链，用 launch 切回目标 App；如果是系统弹窗，请让用户在手机上处理。"
        )


def ad_bands(root: ET.Element) -> list[tuple[int, int]]:
    """Vertical ranges covered by sponsored cells; touches that start there can open other apps."""
    bands = []
    for cell in root.iter("XCUIElementTypeCell"):
        if any(inner is not cell for inner in cell.iter("XCUIElementTypeCell")):
            continue
        if any(AD_TEXT.match(element_text(element)) for element in cell.iter()):
            box = frame(cell)
            if box:
                bands.append((box["y"], box["y"] + box["height"]))
    return bands


def drag_start(root: ET.Element, height: int, distance: int) -> int | None:
    """Lowest start height whose touch-down is not on an ad, leaving room to drag `distance`."""
    bands = ad_bands(root)
    top_limit = int(height * 0.18) + distance
    for y in range(int(height * 0.75), top_limit - 1, -20):
        if not any(low - 10 <= y <= high + 10 for low, high in bands):
            return y
    return None


def touch_drag(run: Path, start: tuple[int, int], end: tuple[int, int], duration_ms: int) -> None:
    """One finger: press at `start`, move to `end` over `duration_ms`, lift (W3C actions)."""
    steps = [
        {"type": "pointerMove", "duration": 0, "x": start[0], "y": start[1]},
        {"type": "pointerDown", "button": 0},
        {"type": "pointerMove", "duration": duration_ms, "x": end[0], "y": end[1]},
        {"type": "pointerUp", "button": 0},
    ]
    session_request(run, "POST", "/actions", {"actions": [
        {"type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"}, "actions": steps},
    ]})


def drag_up(run: Path, width: int, height: int, root: ET.Element | None = None, duration_ms: int = 600, x_ratio: float = 0.82) -> bool:
    """Move content up with one slow drag near the right edge; False if every start point is on an ad.

    `mobile: swipe` presses the middle of the screen, which in feeds and comment lists is
    often a link card or an ad; a slow drag from the side, starting outside sponsored cells,
    is not read as a tap.
    """
    distance = int(height * 0.40)
    start = drag_start(root, height, distance) if root is not None else int(height * 0.72)
    if start is None:
        return False
    x = int(width * x_ratio)
    touch_drag(run, (x, start), (x, start - distance), duration_ms)
    return True


def tap_at(run: Path, x: int, y: int) -> None:
    execute_script(run, "mobile: tap", [{"x": x, "y": y}])


def cmd_tap(args: argparse.Namespace) -> None:
    run = require_run(args.run)
    require_target_app(run)
    tap_at(run, args.x, args.y)
    log_action(args, {"type": "tap", "x": args.x, "y": args.y, "reason": args.reason})


def cmd_tap_ratio(args: argparse.Namespace) -> None:
    """Tap at a fraction of the screen, so no point/pixel conversion is needed."""
    for name in ("x_ratio", "y_ratio"):
        if not 0.0 <= getattr(args, name) <= 1.0:
            raise SystemExit(f"{name} 必须在 0 到 1 之间")
    run = require_run(args.run)
    require_target_app(run)
    width, height = window_size(run)
    x, y = round(args.x_ratio * (width - 1)), round(args.y_ratio * (height - 1))
    tap_at(run, x, y)
    log_action(args, {
        "type": "tap", "x": x, "y": y, "coordinate_mode": "screen_ratio",
        "x_ratio": args.x_ratio, "y_ratio": args.y_ratio, "screen_size": [width, height], "reason": args.reason,
    })


def cmd_swipe(args: argparse.Namespace) -> None:
    run = require_run(args.run)
    require_target_app(run)
    touch_drag(run, (args.x1, args.y1), (args.x2, args.y2), args.duration_ms)
    log_action(args, {"type": "swipe", "from": [args.x1, args.y1], "to": [args.x2, args.y2], "duration_ms": args.duration_ms, "reason": args.reason})


def cmd_swipe_check(args: argparse.Namespace) -> None:
    """swipe, wait, screenshot, and keep the screenshot only if the page moved."""
    run = require_run(args.run)
    before = latest_screenshot(run)
    if before is None:
        raise SystemExit("先截一张当前页面，swipe-check 才有可比较的对象")
    cmd_swipe(args)
    time.sleep(args.wait_seconds)
    judge_scroll(run, before, capture(run, args.title, args.reason, args.with_ui), args.threshold)


def cmd_texts(args: argparse.Namespace) -> None:
    items = page_texts(page_source(require_run(args.run)), include_hidden=args.include_hidden)
    if args.query:
        query = args.query.lower()
        items = [item for item in items if query in item["text"].lower()]
    if args.min_length:
        items = [item for item in items if len(item["text"]) >= args.min_length]
    print(json.dumps(items, ensure_ascii=False, indent=2))


def cmd_tap_text(args: argparse.Namespace) -> None:
    run = require_run(args.run)
    require_target_app(run)
    query = args.text.strip()
    if not query:
        raise SystemExit("tap-text 需要非空文字")
    if not args.allow_risky and any(word in query.lower() for word in RISKY_WORDS):
        raise SystemExit(f"“{query}”可能触发购买、发布、删除或领取等外部操作。请先向用户确认，再加 --allow-risky。")
    items = [item for item in page_texts(page_source(run), dedupe=False) if item["width"] > 0 and item["height"] > 0]
    matches = [item for item in items if item["text"] == query]
    if not matches and not args.exact:
        matches = [item for item in items if query in item["text"]]
    matches = innermost(matches)
    if not matches:
        raise SystemExit(f"当前页面没有可见的“{query}”。请先截图或运行 texts 确认。")
    if len(matches) > 1 and args.index is None:
        listing = "\n".join(f"[{n}] {m['type']} {m['text'][:40]} @({m['x']},{m['y']},{m['width']}x{m['height']})" for n, m in enumerate(matches))
        raise SystemExit(f"匹配到 {len(matches)} 处“{query}”，请用 --index 指定：\n{listing}")
    index = args.index or 0
    if not 0 <= index < len(matches):
        raise SystemExit(f"--index 超出范围：0..{len(matches) - 1}")
    target = matches[index]
    if not args.allow_risky and len(target["text"]) <= 12 and any(word in target["text"].lower() for word in RISKY_WORDS):
        raise SystemExit(f"目标“{target['text']}”可能触发外部操作。请先向用户确认，再加 --allow-risky。")
    x = target["x"] + target["width"] // 2
    y = target["y"] + target["height"] // 2
    execute_script(run, "mobile: tap", [{"x": x, "y": y}])
    log_action(args, {"type": "tap", "x": x, "y": y, "coordinate_mode": "points", "target_text": target["text"][:60], "reason": args.reason})


def cmd_scroll(args: argparse.Namespace) -> None:
    """Scroll content in the named direction; `down` reveals content further down the page."""
    run = require_run(args.run)
    require_target_app(run)
    previous = latest_screenshot(run) if args.check else None
    if args.check and previous is None:
        raise SystemExit("先截一张当前页面，scroll --check 才有可比较的对象")
    finger = {"down": "up", "up": "down", "right": "left", "left": "right"}[args.direction]
    execute_script(run, "mobile: swipe", [{"direction": finger, "velocity": args.velocity}])
    append_event(run, {"kind": "action", "type": "scroll", "direction": args.direction, "velocity": args.velocity, "reason": args.reason})
    # Lazily rendered feeds (for example 酷安) expose post text only after layout settles.
    time.sleep(args.wait_seconds)
    if previous is None:
        print(json.dumps({"type": "scroll", "direction": args.direction, "reason": args.reason}, ensure_ascii=False))
        return
    candidate = capture(run, args.title, args.reason)
    judge_scroll(run, previous, candidate, args.threshold)


def tap_point(box: dict[str, int] | None, screen_height: int, safe_top: int, safe_bottom: int) -> tuple[int, int] | None:
    """A point inside the visible part of a post body, away from bars at the top and bottom."""
    if not box:
        return None
    top, bottom = max(box["y"], safe_top), min(box["y"] + box["height"], screen_height - safe_bottom)
    if bottom - top < 30:
        return None
    return box["x"] + box["width"] // 2, (top + bottom) // 2


def cmd_collect(args: argparse.Namespace) -> None:
    """Scroll a feed and save each distinct post once to RUN_DIR/collected.jsonl.

    With --comments N, each new post is opened by tapping its body text, up to N comments
    are read from the detail page, and the in-app back button returns to the feed.
    """
    run = require_run(args.run)
    if not 1 <= args.screens <= 60:
        raise SystemExit("--screens 必须在 1 到 60 之间")
    if not 0 <= args.comments <= 200:
        raise SystemExit("--comments 必须在 0 到 200 之间")
    out = run / "collected.jsonl"
    items: dict[str, dict[str, Any]] = {}
    if out.exists():
        for line in out.read_text(encoding="utf-8").split("\n"):
            if line.strip():
                item = json.loads(line)
                items[post_key(item["body"])] = item

    def save() -> None:
        # Written after every screen, so an interrupted run keeps what it already read.
        with out.open("w", encoding="utf-8") as handle:
            for item in sorted(items.values(), key=lambda value: value["id"]):
                handle.write(json.dumps(saved(item), ensure_ascii=False) + "\n")

    def stable_posts() -> list[dict[str, Any]]:
        # Poll until two consecutive reads agree, so half-rendered cells are not saved.
        previous: set[str] | None = None
        posts: list[dict[str, Any]] = []
        for _ in range(args.settle_polls):
            posts = parse_posts(page_source(run), args.min_length)
            keys = {post_key(post["body"]) for post in posts}
            if keys and keys == previous:
                break
            previous = keys
            time.sleep(args.settle_seconds)
        return posts

    def guard(item: dict[str, Any] | None = None) -> None:
        """Stop as soon as another app is in front; switch back but never act inside it."""
        current = foreground_bundle(run)
        if not current or current == bundle:
            return
        execute_script(run, "mobile: activateApp", [{"bundleId": bundle}])
        if item is not None:
            item["comment_status"] = "left_app"
        append_event(run, {"kind": "left_app", "foreground": current, "post": item["id"] if item else None})
        save()
        raise SystemExit(
            f"操作后前台变成了 {current}（可能点到了外链），已切回 {bundle} 并停止采集，已采集的内容已保存。"
            "请截图确认当前页面，回到列表后用同一个 RUN_DIR 重新运行 collect；这条帖子会被跳过。"
        )

    def back_to_feed(item: dict[str, Any]) -> None:
        for _ in range(2):
            root = page_source(run)
            point = back_button(root)
            if point is None:
                if parse_posts(root, args.min_length):
                    return
                break
            guard(item)  # an earlier touch may have opened another app with a delay
            execute_script(run, "mobile: tap", [{"x": point[0], "y": point[1]}])
            time.sleep(args.settle_seconds * 1.5)
            guard(item)
            if back_button(page_source(run)) is None:
                return
        save()
        raise SystemExit("打开帖子后没能回到列表。已保存已采集的内容；请截图确认当前页面，回到列表后用同一个 RUN_DIR 重新运行 collect。")

    def read_detail(item: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None, int | None]:
        marker = post_key(item["body"])[:16]
        comments: dict[tuple[str, str], dict[str, Any]] = {}
        full_body = None
        total = None
        last_signature = None
        # Long posts push comments down, so screens spent reaching the first comment do not
        # count against the budget (about 3 comments per screen, unless --comment-screens is set).
        budget = args.comment_screens or min(30, args.comments // 3 + 2)
        seek_limit = 10
        screens_with_comments = 0
        unchanged = 0
        for _ in range(seek_limit + budget + 1):
            root = page_source(run)
            for text in (entry["text"] for entry in page_texts(root)):
                if full_body is None and marker and post_key(text).startswith(marker):
                    full_body = text
                found = re.search(r"共\s*(\d+)\s*(?:回复|条评论|评论)", text)
                if total is None and found:
                    total = int(found.group(1))
            for comment in parse_comments(root, item["body"]):
                comments.setdefault((comment["author"], post_key(comment["body"])[:40]), comment)
            if comments:
                screens_with_comments += 1
            signature = page_signature(root)
            unchanged = unchanged + 1 if signature == last_signature else 0
            # One unchanged read can mean the drag started on an image; two in a row is the bottom.
            if len(comments) >= args.comments or unchanged >= 2:
                break
            if screens_with_comments > budget or (not comments and total == 0):
                break
            last_signature = signature
            guard(item)
            # After a drag that did not move the page (it started on an image), try the other side.
            if not drag_up(run, screen_width, screen_height, root, x_ratio=0.18 if unchanged else 0.82):
                break  # ads cover every start point; stop rather than touch one
            time.sleep(args.wait_seconds)
            guard(item)
        picked = list(comments.values())[: args.comments]
        for number, comment in enumerate(picked, 1):
            comment["id"] = f"{item['id']}-c{number:02d}"
        return picked, full_body, total

    def open_post(item: dict[str, Any], point: tuple[int, int]) -> None:
        guard()
        execute_script(run, "mobile: tap", [{"x": point[0], "y": point[1]}])
        status = "no_detail"
        for _ in range(4):
            time.sleep(args.settle_seconds)
            guard(item)
            root = page_source(run)
            if back_button(root) is None:
                continue
            status = "ok" if is_post_detail(root, item["body"]) else "wrong_page"
            if status == "ok":
                break
        if status == "ok":
            if opened == 0:
                capture(run, f"帖子详情示例：{item['id']}", "记录评论采集时打开的详情页")
            comments, full_body, total = read_detail(item)
            item["comments"] = comments
            item["comment_total"] = total
            if full_body and len(full_body) > len(item["body"]):
                item["full_body"] = full_body
        item["comment_status"] = status
        append_event(run, {"kind": "collect_open", "id": item["id"], "status": status, "comments": len(item.get("comments") or [])})
        if status != "no_detail" or back_button(page_source(run)) is not None:
            back_to_feed(item)

    bundle = target_bundle(run)
    require_target_app(run)
    append_event(run, {"kind": "action", "type": "collect", "label": args.label, "max_screens": args.screens, "comments": args.comments, "reason": args.reason})
    capture(run, f"采集起点：{args.label}", "记录采集开始时的页面")
    screen_width, screen_height = window_size(run)
    stale = 0
    screens_done = 0
    reached_end = False
    opened = 0
    # Posts that already had a comment status (from an earlier pass on this RUN_DIR) are not
    # counted again; the summary covers only posts this pass handled.
    handled_before = {key for key, item in items.items() if "comment_status" in item}
    seen: set[str] = set()
    for screen in range(1, args.screens + 1):
        posts = stable_posts()
        new = 0
        for post in posts:
            key = post_key(post["body"])
            seen.add(key)
            if key in items:
                merge_post(items[key], saved(post))
                continue
            items[key] = {"id": f"p{len(items) + 1:03d}", "screen": screen, "source": args.label, **saved(post)}
            new += 1
        if args.comments:
            # Positions shift after each round trip, so re-read the feed before every open.
            for _ in range(8):
                target = None
                for post in posts:
                    item = items.get(post_key(post["body"]))
                    point = tap_point(post.get("_frame"), screen_height, args.safe_top, args.safe_bottom)
                    if item and "comment_status" not in item and point:
                        target = (item, point)
                        break
                if target is None:
                    break
                open_post(*target)
                opened += 1
                time.sleep(args.settle_seconds)
                posts = stable_posts()
        screens_done = screen
        append_event(run, {"kind": "collect_screen", "screen": screen, "found": len(posts), "new": new, "total": len(items)})
        save()
        stale = stale + 1 if new == 0 else 0
        if stale >= 2:
            reached_end = True
            break
        if screen < args.screens:
            guard()
            if not drag_up(run, screen_width, screen_height, page_source(run)):
                break
            time.sleep(args.wait_seconds)
            guard()
        if args.every_screen:
            capture(run, f"采集第 {screen} 屏", "记录采集过程")
    save()
    if args.comments:
        # Posts seen in this pass that never got a usable tap point (e.g. cut off at the last screen).
        for key in seen:
            if "comment_status" not in items[key]:
                items[key]["comment_status"] = "not_opened"
        save()
    mine = [key for key in seen if key not in handled_before]
    statuses = [items[key].get("comment_status") for key in mine] if args.comments else []
    comment_count = sum(len(items[key].get("comments") or []) for key in mine)
    summary = {
        "label": args.label, "screens": screens_done, "total": len(items), "reached_end": reached_end,
        "comments_per_post": args.comments, "opened": statuses.count("ok"),
        "open_failed": sum(1 for status in statuses if status in ("wrong_page", "no_detail", "left_app")),
        "not_opened": statuses.count("not_opened"),
        "comments": comment_count,
    }
    append_event(run, {"kind": "collect_done", **summary})
    capture(run, f"采集终点：{args.label}", "记录采集结束时的页面")
    print(json.dumps({"type": "collect", **summary, "file": str(out)}, ensure_ascii=False))


SENSITIVE_INPUT = ("password", "passwd", "otp", "验证码", "密码", "身份证", "银行卡")


def cmd_input(args: argparse.Namespace) -> None:
    """Type into whatever field has focus; the text itself is not written to the log."""
    lowered = args.text.lower()
    if any(word in lowered for word in SENSITIVE_INPUT):
        raise SystemExit("这段文字像是密码、验证码或证件信息，不会输入。请改用普通的测试文字。")
    run = require_run(args.run)
    require_target_app(run)
    session_request(run, "POST", "/keys", {"value": list(args.text)})
    log_action(args, {"type": "input_text", "text_length": len(args.text), "reason": args.reason})


def cmd_key(args: argparse.Namespace) -> None:
    """HOME leaves the app; ENTER submits the focused field. iPhone has no system BACK."""
    key = args.key.upper()
    run = require_run(args.run)
    if key == "HOME":
        execute_script(run, "mobile: pressButton", [{"name": "home"}])
    elif key == "ENTER":
        session_request(run, "POST", "/keys", {"value": ["\uE007"]})  # WebDriver code for Return
    elif key == "BACK":
        raise SystemExit("iPhone 没有系统返回键：点 App 自己的返回或关闭按钮，再截图确认。")
    else:
        raise SystemExit(f"不支持的按键 {args.key}；可用 HOME 或 ENTER。")
    log_action(args, {"type": "key", "key": key, "reason": args.reason})


def cmd_wait(args: argparse.Namespace) -> None:
    if args.seconds < 0 or args.seconds > 10:
        raise SystemExit("一次最多等 10 秒；要等更久就分几次，中间截图看看页面。")
    time.sleep(args.seconds)
    log_action(args, {"type": "wait", "seconds": args.seconds, "reason": args.reason})


def cmd_launch(args: argparse.Namespace) -> None:
    """Bring the run's app (or --bundle-id) to the front."""
    run = require_run(args.run)
    bundle_id = args.bundle_id or target_bundle(run)
    session_request(run, "POST", "/appium/device/activate_app", {"bundleId": bundle_id})
    log_action(args, {"type": "launch", "bundle_id": bundle_id, "reason": args.reason})


def cmd_close(args: argparse.Namespace) -> None:
    """End the Appium session and mark the run closed, even if the session already expired."""
    run = require_run(args.run)
    session = session_info(run)
    try:
        appium_request(str(session["server"]), "DELETE", f"/session/{session['session_id']}", timeout=30)
    finally:
        manifest = read_json(run / "manifest.json")
        manifest.update(status="closed", closed_at=now())
        write_json(run / "manifest.json", manifest)
        append_event(run, {"kind": "run_closed"})
    print(run)


# Command-line interface. Each entry: name -> (handler, help, arguments), where every
# argument is (flags, add_argument keyword options). Most commands also take --run and
# --reason; NEEDS_RUN / NEEDS_REASON list the ones that do.

XY = [(("x1",), {"type": int}), (("y1",), {"type": int}), (("x2",), {"type": int}), (("y2",), {"type": int})]
CHECK = [
    (("--threshold",), {"type": float, "default": 0.02, "help": "画面变化低于这个值就当作没动，丢弃截图"}),
]

COMMANDS: dict[str, tuple[Any, str, list[tuple[tuple[str, ...], dict[str, Any]]]]] = {
    "status": (cmd_status, "列出已连接的 iPhone 并检查 Appium", []),
    "screen-size": (cmd_screen_size, "屏幕的 point 尺寸、截图像素和缩放比", []),
    "start-run": (cmd_start_run, "打开目标 App 并新建一次走查，输出 RUN_DIR", [
        (("--title",), {"required": True}),
        (("--task",), {"required": True}),
        (("--bundle-id",), {"default": os.environ.get("IOS_BUNDLE_ID")}),
        (("--udid",), {}),
        (("--team-id",), {}),
        (("--signing-id",), {}),
        (("--wda-bundle-id",), {}),
        (("--appium-server",), {"default": APPIUM_SERVER}),
        (("--start-appium",), {"action": argparse.BooleanOptionalAction, "default": True}),
    ]),
    "screenshot": (cmd_screenshot, "截图并记录为证据", [
        (("--title",), {"required": True}),
        (("--with-ui",), {"action": "store_true", "help": "同时保存页面结构 XML"}),
    ]),
    "dump-ui": (cmd_dump_ui, "保存当前页面的完整 XCUITest 结构", []),
    "tap": (cmd_tap, "按 point 坐标点击", [(("x",), {"type": int}), (("y",), {"type": int})]),
    "tap-ratio": (cmd_tap_ratio, "按屏幕比例（0–1）点击", [(("x_ratio",), {"type": float}), (("y_ratio",), {"type": float})]),
    "swipe": (cmd_swipe, "自定义滑动（point 坐标）", XY + [(("--duration-ms",), {"type": int, "default": 450})]),
    "swipe-check": (cmd_swipe_check, "滑动后截图，页面没变就丢弃截图", XY + CHECK + [
        (("--duration-ms",), {"type": int, "default": 450}),
        (("--wait-seconds",), {"type": float, "default": 0.8}),
        (("--title",), {"required": True}),
        (("--with-ui",), {"action": "store_true"}),
    ]),
    "texts": (cmd_texts, "列出当前页面可见文字及 point 坐标", [
        (("--query",), {}),
        (("--min-length",), {"type": int, "default": 0}),
        (("--include-hidden",), {"action": "store_true"}),
    ]),
    "tap-text": (cmd_tap_text, "点击当前页面上可见的文字", [
        (("text",), {}),
        (("--exact",), {"action": "store_true"}),
        (("--index",), {"type": int}),
        (("--allow-risky",), {"action": "store_true", "help": "仅在用户明确同意这个动作后使用"}),
    ]),
    "scroll": (cmd_scroll, "按内容方向滚动；down 表示查看下方内容", CHECK + [
        (("direction",), {"choices": ["down", "up", "left", "right"]}),
        (("--velocity",), {"type": int, "default": 900}),
        (("--wait-seconds",), {"type": float, "default": 2.0}),
        (("--check",), {"action": "store_true", "help": "滚动后截图，和上一张比较"}),
        (("--title",), {"default": "滚动后页面"}),
    ]),
    "collect": (cmd_collect, "滚动列表并按帖子去重保存到 collected.jsonl", [
        (("--label",), {"required": True, "help": "采集范围说明，例如“PCDN / 动态 / 综合排序”"}),
        (("--screens",), {"type": int, "default": 10}),
        (("--min-length",), {"type": int, "default": 20}),
        (("--wait-seconds",), {"type": float, "default": 1.5}),
        (("--settle-polls",), {"type": int, "default": 4}),
        (("--settle-seconds",), {"type": float, "default": 1.0}),
        (("--every-screen",), {"action": "store_true"}),
        (("--comments",), {"type": int, "default": 0, "help": "每条帖子采集的评论数；0 表示只采集列表，不打开帖子"}),
        (("--comment-screens",), {"type": int, "default": 0, "help": "每条帖子详情页最多滚动几屏；默认按 --comments 自动计算"}),
        (("--safe-top",), {"type": int, "default": 200, "help": "点击帖子时避开顶部搜索栏和标签的高度（point）"}),
        (("--safe-bottom",), {"type": int, "default": 100, "help": "点击帖子时避开底部导航的高度（point）"}),
    ]),
    "input": (cmd_input, "在当前输入框里打字", [(("--text",), {"required": True})]),
    "key": (cmd_key, "按 HOME 或 ENTER", [(("key",), {})]),
    "wait": (cmd_wait, "等待页面稳定（最多 10 秒）", [(("--seconds",), {"type": float, "default": 1})]),
    "launch": (cmd_launch, "把目标 App 切回前台", [(("--bundle-id",), {})]),
    "close": (cmd_close, "结束 Appium session", []),
}
NEEDS_RUN = set(COMMANDS) - {"status", "start-run"}
NEEDS_REASON = NEEDS_RUN - {"screen-size", "dump-ui", "texts", "close"}


def build_parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="通过 Appium/XCUITest 操作 USB 连接的 iPhone，每一步都记录到 RUN_DIR")
    commands = cli.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, (handler, summary, options) in COMMANDS.items():
        command = commands.add_parser(name, help=summary, description=summary)
        if name in NEEDS_RUN:
            command.add_argument("--run", required=True, help="start-run 输出的目录")
        for flags, settings in options:
            command.add_argument(*flags, **settings)
        if name in NEEDS_REASON:
            command.add_argument("--reason", required=True, help="这一步为什么做，会写进报告")
        command.set_defaults(func=handler)
    return cli


def main(argv: list[str] | None = None) -> None:
    options = build_parser().parse_args(argv)
    options.func(options)


if __name__ == "__main__":
    main()
