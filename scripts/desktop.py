#!/usr/bin/env python3
"""Drive a macOS desktop app walkthrough through the Accessibility API and screencapture.

Companion to device.py (iPhone/Appium): the same run-directory conventions apply,
so report.py can render a desktop run from the same events.jsonl + evidence/ files.
No Appium, no typing: the only mutating action is kAXPress on a matched element.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import device  # noqa: E402  # reuse run/evidence helpers without pulling in Appium

from device import (  # noqa: E402
    append_event,
    evidence_number,
    judge_scroll,
    latest_screenshot,
    new_run_dir,
    now,
    png_size,
    screen_change,  # noqa: F401  # exported for callers and smoke tests
    write_json,
)

HELPER_SOURCE_NAME = "ax_helper.swift"
CACHE_DIR = Path.home() / "Library" / "Caches" / "app-walkthrough" / "ax"
HELPER_TIMEOUT = 60
OSASCRIPT_TIMEOUT = 30
SCREENCAPTURE_TIMEOUT = 60
LAUNCH_WAIT_SECONDS = 20

# 风险文案与 device.RISKY_WORDS 对齐，并补充桌面端动词。
_DESKTOP_RISKY_WORDS = {
    "order",
    "publish",
    "logout",
    "withdraw",
    "buy",
    "下单",
    "提现",
    "退出登录",
    "注销",
}
RISKY_WORDS = tuple(sorted(set(device.RISKY_WORDS).union(_DESKTOP_RISKY_WORDS)))

AX_PERMISSION_HINT = (
    "缺少辅助功能（Accessibility）权限：请打开 系统设置 > 隐私与安全性 > 辅助功能，"
    "允许运行本脚本的程序（Terminal / iSelf / opencode 等），然后重试。"
)


def fail(message: str) -> None:
    raise SystemExit(message)


def ensure_helper() -> Path:
    """Compile ax_helper.swift on demand; reuse a cached binary keyed by source hash.

    Same cache/locking pattern as ocr.py: a content-addressed binary and a build
    lock so parallel cold starts never run competing swiftc processes.
    """
    source_path = HERE / HELPER_SOURCE_NAME
    if not source_path.exists():
        fail(f"找不到 Swift 辅助程序源码：{source_path}")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()[:12]
    binary_path = CACHE_DIR / f"ax_helper_{digest}"
    if binary_path.exists() and os.access(binary_path, os.X_OK):
        return binary_path

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"无法创建缓存目录 {CACHE_DIR}：{exc}")

    lock_path = CACHE_DIR / ".build.lock"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if binary_path.exists() and os.access(binary_path, os.X_OK):
            return binary_path
        fd, tmp_name = tempfile.mkstemp(prefix=".ax_helper_build_", dir=str(CACHE_DIR))
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
                fail(f"编译 ax_helper.swift 失败：{tail}")
            os.chmod(tmp_path, 0o755)
            os.replace(str(tmp_path), str(binary_path))
        except FileNotFoundError as exc:
            fail(f"找不到 xcrun/swiftc 工具链，无法编译辅助程序：{exc}")
        finally:
            tmp_path.unlink(missing_ok=True)
    return binary_path


def run_helper(*arguments: Any) -> Any:
    """Invoke the helper and parse its JSON; translate {"error": ...} into Chinese exits."""
    helper = ensure_helper()
    command = [str(helper), *[str(arg) for arg in arguments]]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=HELPER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        fail(f"辅助程序超时（{HELPER_TIMEOUT} 秒）：{' '.join(command)}")
    except OSError as exc:
        fail(f"无法执行辅助程序：{exc}")
    stdout = (completed.stdout or "").strip()
    try:
        payload = json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        fail(f"辅助程序返回了非 JSON 内容：{stdout[:500]}")
    if isinstance(payload, dict) and payload.get("error"):
        detail = str(payload["error"])
        if "辅助功能" in detail or "Accessibility" in detail:
            fail(AX_PERMISSION_HINT)
        fail(f"辅助程序报错：{detail}")
    if completed.returncode != 0:
        fail(
            f"辅助程序退出码 {completed.returncode}：{(completed.stderr or stdout).strip()[:500]}"
        )
    return payload


def check_accessibility(pid: int) -> None:
    """A dump doubles as the trusted-process probe; it fails fast without permission."""
    run_helper("dump", pid)


def run_osascript(script: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=OSASCRIPT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return result.returncode, (result.stdout or result.stderr or "").strip()


def find_pid(app_name: str) -> int | None:
    """Locate a running app: exact pgrep first, then System Events, then a loose pgrep."""
    pgrep = shutil.which("pgrep")
    if pgrep:
        exact = subprocess.run([pgrep, "-x", app_name], capture_output=True, text=True)
        if exact.returncode == 0 and exact.stdout.strip():
            return int(exact.stdout.split()[0])
    code, output = run_osascript(
        f'tell application "System Events" to unix id of (first process whose name is "{app_name}")'
    )
    if code == 0 and output.isdigit():
        return int(output)
    if pgrep:
        loose = subprocess.run(
            [pgrep, "-i", "-f", app_name], capture_output=True, text=True
        )
        if loose.returncode == 0 and loose.stdout.strip():
            return int(loose.stdout.split()[0])
    return None


def bundle_id_of(app_name: str) -> str | None:
    code, output = run_osascript(f'id of app "{app_name}"')
    return output if code == 0 and output else None


def launch_app(app_name: str, bundle_id: str | None) -> int:
    """Open the app and wait for its process; never fails silently into a half-started run."""
    target = ["open"]
    if bundle_id:
        target += ["-b", bundle_id]
    else:
        target += ["-a", app_name]
    try:
        subprocess.run(
            target,
            capture_output=True,
            text=True,
            timeout=OSASCRIPT_TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        fail(f"无法启动应用 {app_name}：{exc}")
    deadline = time.time() + LAUNCH_WAIT_SECONDS
    while time.time() < deadline:
        pid = find_pid(app_name)
        if pid is not None:
            return pid
        time.sleep(0.5)
    fail(
        f"已执行 open，但 {LAUNCH_WAIT_SECONDS} 秒内仍未发现 {app_name} 的进程，请确认应用名称正确。"
    )


def app_windows(pid: int) -> list[dict[str, Any]]:
    windows = run_helper("windows", pid)
    return windows if isinstance(windows, list) else []


def require_run(path: str) -> Path:
    run = Path(path).expanduser().resolve()
    if not run.joinpath("manifest.json").is_file():
        fail(f"{run} 不是 start-run 创建的目录（缺少 manifest.json）")
    run.joinpath("evidence").mkdir(exist_ok=True)
    return run


def load_manifest(run: Path) -> dict[str, Any]:
    return json.loads((run / "manifest.json").read_text(encoding="utf-8"))


def target_pid(run: Path) -> int:
    manifest = load_manifest(run)
    pid = manifest.get("pid") or (manifest.get("app") or {}).get("pid")
    if not isinstance(pid, int):
        fail("manifest.json 里没有记录应用 pid，请重新 start-run。")
    return pid


def next_evidence_number(run: Path) -> int:
    """Max existing evidence number + 1, falling back to the event count (device.py style)."""
    shots = list((run / "evidence").glob("evidence-*.png"))
    if shots:
        return max(evidence_number(path) for path in shots) + 1
    return device.event_count(run) + 1


def activate_app(app_name: str) -> None:
    run_osascript(f'tell application "{app_name}" to activate')
    time.sleep(0.4)


def cmd_start_run(args: argparse.Namespace) -> None:
    app_name = args.app_name.strip()
    if not app_name:
        fail("start-run 需要非空 --app-name")
    bundle_id = args.bundle_id or bundle_id_of(app_name)
    pid = find_pid(app_name)
    launched = False
    if pid is None:
        print(f"应用 {app_name} 未运行，正在启动…", file=sys.stderr)
        pid = launch_app(app_name, bundle_id)
        launched = True
        time.sleep(1.5)  # give the app a moment to create its first window

    # 先探测辅助功能权限再创建目录，权限不足时不留下半成品 run。
    check_accessibility(pid)

    windows = app_windows(pid)
    window = windows[0] if windows else None
    run = new_run_dir(args.title)
    manifest = {
        "title": args.title,
        "task": args.task,
        "platform": "macos",
        "app": {"name": app_name, "pid": pid, "bundle_id": bundle_id},
        "pid": pid,
        "bundle_id": bundle_id,
        "window": window,
        "window_id": window.get("window_id") if window else None,
        "launched_by_driver": launched,
        "started_at": now(),
        "status": "running",
    }
    write_json(run / "manifest.json", manifest)
    append_event(
        run,
        {
            "kind": "run_started",
            "title": args.title,
            "task": args.task,
            "platform": "macos",
            "app": app_name,
            "pid": pid,
            "bundle_id": bundle_id,
        },
    )

    elements = run_helper("dump", pid)
    envelope = {"platform": "macos", "app": app_name, "pid": pid, "elements": elements}
    (run / "ui-latest.json").write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    append_event(run, {"kind": "ui_dump", "path": "ui-latest.json"})
    if not windows:
        print("提示：暂时没有取到窗口 ID，截图将回退为全屏截图。", file=sys.stderr)
    print(run)


def capture_screenshot(run: Path, title: str, reason: str) -> Path:
    manifest = load_manifest(run)
    pid = target_pid(run)
    number = next_evidence_number(run)
    stem = f"evidence-{number:03d}-{datetime.now():%Y%m%d-%H%M%S}"
    folder = run / "evidence"
    image_path = folder / f"{stem}.png"
    window_id = manifest.get("window_id")

    scope = "window" if window_id is not None else "screen"
    warning = None if window_id is not None else "没有窗口 ID，使用全屏截图（scope=screen）"
    command = ["screencapture", "-x", "-o"]
    if window_id is not None:
        command += ["-l", str(window_id), str(image_path)]
    else:
        command.append(str(image_path))
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=SCREENCAPTURE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = None
        fail(f"截图命令执行失败：{exc}")
    if result is not None and result.returncode != 0 or not image_path.exists():
        # 窗口截图可能因窗口关闭/最小化失败，回退为全屏截图。
        warning = f"窗口截图失败（{(result.stderr if result else '').strip()[:200]}），已回退为全屏截图"
        scope = "screen"
        window_id = None
        fallback = ["screencapture", "-x", "-o", str(image_path)]
        subprocess.run(
            fallback, capture_output=True, text=True, timeout=SCREENCAPTURE_TIMEOUT
        )
    if not image_path.exists():
        fail("截图失败：screencapture 没有生成图片，请确认已授予屏幕录制权限。")

    width, height = png_size(image_path.read_bytes())
    record = {
        "number": number,
        "step": number,
        "title": title,
        "reason": reason,
        "image": image_path.name,
        "platform": "macos",
        "scope": scope,
        "window": manifest.get("window"),
        "window_id": window_id if scope == "window" else None,
        "image_width": width,
        "image_height": height,
        "timestamp": now(),
    }
    if warning:
        record["warning"] = warning
    write_json(folder / f"{stem}.json", record)
    append_event(run, {"kind": "evidence", **record})
    if warning:
        print(f"提示：{warning}", file=sys.stderr)
    print(image_path)
    return image_path


def cmd_screenshot(args: argparse.Namespace) -> None:
    capture_screenshot(require_run(args.run), args.title, args.reason)


def cmd_texts(args: argparse.Namespace) -> None:
    run = require_run(args.run)
    pid = target_pid(run)
    app_name = load_manifest(run)["app"]["name"]
    elements = run_helper("dump", pid)
    envelope = {"platform": "macos", "app": app_name, "pid": pid, "elements": elements}
    (run / "ui-latest.json").write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    append_event(
        run, {"kind": "ui_dump", "path": "ui-latest.json", "count": len(elements)}
    )
    print(json.dumps(envelope, ensure_ascii=False, indent=2))


def _element_texts(element: dict[str, Any]) -> list[str]:
    return [
        str(element[key])
        for key in ("title", "value", "description")
        if element.get(key)
    ]


def _is_risky(text: str) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in RISKY_WORDS)


def match_elements(elements: list, query: str, exact: bool = False) -> tuple:
    exact_matches, contains_matches = [], []
    for element in elements:
        fields = _element_texts(element)
        if query in fields:
            exact_matches.append(element)
        elif not exact and any(query.casefold() in field.casefold() for field in fields):
            contains_matches.append(element)
    return exact_matches, contains_matches


def cmd_tap_text(args: argparse.Namespace) -> None:
    run = require_run(args.run)
    pid = target_pid(run)
    query = args.text.strip()
    if not query:
        fail("tap-text 需要非空文字")
    if not args.allow_risky and _is_risky(query):
        fail(
            f"“{query}”可能触发支付、发布、删除或退出登录等外部操作。请先向用户确认，再加 --allow-risky。"
        )

    elements = run_helper("dump", pid)
    exact, contains = match_elements(elements, query, exact=args.exact)
    matches = exact if exact else ([] if args.exact else contains)
    if not matches:
        fail(f"当前页面没有可见的“{query}”。请先截图或运行 texts 确认。")
    index = args.index or 0
    if not 0 <= index < len(matches):
        fail(f"--index 超出范围：0..{len(matches) - 1}（共匹配 {len(matches)} 处）")
    target = matches[index]
    target_label = next(iter(_element_texts(target)), query)
    if not args.allow_risky and _is_risky(target_label):
        fail(
            f"目标“{target_label}”可能触发外部操作。请先向用户确认，再加 --allow-risky。"
        )

    result = run_helper("press", pid, query, index)
    if not isinstance(result, dict) or not result.get("ok"):
        message = result.get("message") if isinstance(result, dict) else result
        fail(f"点击被拒绝或执行失败：{message}")
    action = {
        "type": "tap",
        "via": "accessibility",
        "target_text": target_label[:60],
        "query": query,
        "match": result.get("selected", {}).get("match"),
        "index": index,
        "matched": result.get("matched"),
        "reason": args.reason,
    }
    append_event(run, {"kind": "action", **action})
    print(json.dumps(action, ensure_ascii=False))


KEY_CODES = {"down": 121, "up": 115, "left": 123, "right": 124}


def cmd_scroll(args: argparse.Namespace) -> None:
    run = require_run(args.run)
    manifest = load_manifest(run)
    pid = target_pid(run)
    previous = latest_screenshot(run) if args.check else None
    if args.check and previous is None:
        fail("先截一张当前页面，scroll --check 才有可比较的对象")
    check_accessibility(pid)

    activate_app(manifest["app"]["name"])
    key_code = KEY_CODES[args.direction]
    code, output = run_osascript(
        f'tell application "System Events" to key code {key_code}'
    )
    if code != 0:
        fail(f"发送滚动按键失败（key code {key_code}）：{output}")
    append_event(
        run,
        {
            "kind": "action",
            "type": "scroll",
            "direction": args.direction,
            "via": "key_code",
            "key_code": key_code,
            "reason": args.reason,
        },
    )
    time.sleep(args.wait_seconds)

    if not args.check:
        print(
            json.dumps(
                {"type": "scroll", "direction": args.direction, "reason": args.reason},
                ensure_ascii=False,
            )
        )
        return
    candidate = capture_screenshot(run, args.title, args.reason)
    judge_scroll(run, previous, candidate, args.threshold)


def cmd_close(args: argparse.Namespace) -> None:
    run = require_run(args.run)
    manifest = load_manifest(run)
    # 只标记 run 关闭，绝不替用户退出桌面 App。
    manifest.update(status="closed", closed_at=now())
    write_json(run / "manifest.json", manifest)
    append_event(run, {"kind": "run_closed"})
    print(run)


def cmd_status(_args: argparse.Namespace) -> None:
    info: dict[str, Any] = {"platform": "macos"}
    try:
        helper = ensure_helper()
        info["helper"] = {"available": True, "binary": str(helper)}
    except SystemExit as exc:
        info["helper"] = {"available": False, "error": str(exc)}
        info["frontmost_apps"] = []
        info["accessibility"] = None
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    # dump 自身进程没有副作用；未授权时同样会返回权限错误。
    try:
        run_helper("dump", os.getpid())
        info["accessibility"] = True
    except SystemExit as exc:
        info["accessibility"] = False
        info["accessibility_hint"] = str(exc)

    code, output = run_osascript(
        'tell application "System Events" to get name of every process whose background only is false'
    )
    if code == 0:
        info["frontmost_apps"] = [
            item.strip() for item in output.split(",") if item.strip()
        ]
    else:
        info["frontmost_apps"] = []
        info["process_list_hint"] = (
            "System Events 查询失败（通常也是辅助功能权限未授予）"
        )
    print(json.dumps(info, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="通过 macOS 辅助功能和 screencapture 走查桌面 App，每一步都记录到 RUN_DIR",
    )
    commands = cli.add_subparsers(dest="command", required=True, metavar="COMMAND")

    start = commands.add_parser(
        "start-run", help="定位或启动桌面 App 并新建一次走查，输出 RUN_DIR"
    )
    start.add_argument(
        "--app-name", required=True, help="应用名称，例如“备忘录”或 Notes"
    )
    start.add_argument("--title", required=True, help="本次走查的短标题")
    start.add_argument("--task", required=True, help="用户要完成的任务")
    start.add_argument("--bundle-id", help="可选 Bundle ID，例如 com.apple.Notes")
    start.set_defaults(func=cmd_start_run)

    shot = commands.add_parser("screenshot", help="截图并记录为证据")
    shot.add_argument("--run", required=True)
    shot.add_argument("--title", required=True)
    shot.add_argument("--reason", required=True, help="这一步为什么截图，会写进报告")
    shot.set_defaults(func=cmd_screenshot)

    texts = commands.add_parser(
        "texts", help="输出当前窗口的辅助功能元素 JSON，并保存 ui-latest.json"
    )
    texts.add_argument("--run", required=True)
    texts.set_defaults(func=cmd_texts)

    tap = commands.add_parser(
        "tap-text", help="按文字找到元素并执行 kAXPress（不会输入任何文字）"
    )
    tap.add_argument("--run", required=True)
    tap.add_argument("text", help="要点击的元素标题/值/描述（精确匹配优先）")
    tap.add_argument("--exact", action="store_true", help="只接受完全相等的匹配")
    tap.add_argument("--index", type=int, help="多个匹配时的零基序号")
    tap.add_argument(
        "--allow-risky", action="store_true", help="仅在用户明确同意该动作后使用"
    )
    tap.add_argument("--reason", required=True, help="这一步为什么点击，会写进报告")
    tap.set_defaults(func=cmd_tap_text)

    scroll = commands.add_parser(
        "scroll", help="用 PageDown/PageUp 等按键滚动；--check 时截图并比较变化"
    )
    scroll.add_argument("--run", required=True)
    scroll.add_argument(
        "direction",
        choices=["down", "up", "left", "right"],
        help="down 表示查看下方内容",
    )
    scroll.add_argument("--reason", required=True)
    scroll.add_argument(
        "--check", action="store_true", help="滚动后截图，和上一张比较，没变就丢弃"
    )
    scroll.add_argument("--title", default="滚动后页面")
    scroll.add_argument(
        "--threshold", type=float, default=0.02, help="画面变化低于该值视为没动"
    )
    scroll.add_argument("--wait-seconds", type=float, default=1.0)
    scroll.set_defaults(func=cmd_scroll)

    close = commands.add_parser("close", help="把走查标记为关闭（不会退出桌面 App）")
    close.add_argument("--run", required=True)
    close.set_defaults(func=cmd_close)

    status = commands.add_parser(
        "status", help="查看辅助功能权限、辅助程序和前台 App，不做任何修改"
    )
    status.set_defaults(func=cmd_status)
    return cli


def main(argv: list[str] | None = None) -> None:
    options = parser().parse_args(argv)
    options.func(options)


if __name__ == "__main__":
    main()
