#!/usr/bin/env python3
"""Walk through a web product in an existing Chrome or Safari window.

The browser keeps its normal profile and login state.  This module only uses
AppleScript to ask the front tab for DOM text/geometry and macOS screencapture
to preserve evidence; no browser automation package is required.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from device import append_event, now, screen_change, write_json  # noqa: E402

RUNS_DIR = SCRIPT_DIR.parent / "runs"
BROWSERS = {"chrome": "Google Chrome", "safari": "Safari"}
RISKY_WORDS = (
    "password",
    "otp",
    "pay",
    "payment",
    "purchase",
    "order",
    "buy",
    "delete",
    "publish",
    "send",
    "subscribe",
    "logout",
    "withdraw",
    "支付",
    "付款",
    "购买",
    "下单",
    "删除",
    "发布",
    "发送",
    "订阅",
    "退出登录",
    "提现",
    "绑定银行卡",
)


def run_osascript(source: str, timeout: float = 30) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-"],
            input=source,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SystemExit("找不到 osascript；网页走查只能在 macOS 上运行") from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"AppleScript 超时（{timeout:g} 秒）") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SystemExit(f"AppleScript 执行失败：{detail}")
    return result.stdout.strip()


def apple_string(value: str) -> str:
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    )


def browser_name(browser: str) -> str:
    return BROWSERS[browser]


def browser_script(browser: str, javascript: str) -> str:
    name = browser_name(browser)
    code = apple_string(javascript)
    if browser == "chrome":
        return f'tell application "{name}" to tell active tab of front window to execute javascript {code}'
    return f'tell application "{name}" to do JavaScript {code} in current tab of front window'


def browser_js(browser: str, javascript: str) -> Any:
    output = run_osascript(browser_script(browser, javascript))
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def page_info(browser: str) -> dict[str, Any]:
    if browser == "chrome":
        source = 'tell application "Google Chrome" to tell active tab of front window to return {URL, title}'
    else:
        source = 'tell application "Safari" to tell current tab of front window to return {URL, name}'
    raw = run_osascript(source)
    parts = raw.split(", ", 1)
    return {
        "url": parts[0] if parts else "",
        "title": parts[1] if len(parts) > 1 else "",
    }


def app_process_window_id(app: str) -> int | None:
    script = f"""tell application "System Events"
    tell process {apple_string(app)}
        if (count of windows) is 0 then return ""
        return value of attribute "AXWindowNumber" of window 1
    end tell
end tell"""
    try:
        value = run_osascript(script, timeout=10)
        if value:
            return int(value)
    except (SystemExit, ValueError):
        pass
    # System Events may be denied even when CGWindowList is available. Reuse
    # the bundled Swift helper to select the largest titled browser window.
    try:
        helper = SCRIPT_DIR / "desktop.py"
        import desktop  # type: ignore

        pgrep = subprocess.run(
            ["pgrep", "-x", app], capture_output=True, text=True, check=False
        )
        pid = int(pgrep.stdout.splitlines()[0]) if pgrep.stdout.strip() else None
        if pid:
            windows = desktop.run_helper("windows", pid)
            candidates = [
                w
                for w in windows
                if w.get("width", 0) > 500 and w.get("height", 0) > 300
            ]
            if candidates:
                titled = [w for w in candidates if w.get("title")]
                return max(
                    titled or candidates,
                    key=lambda w: w.get("width", 0) * w.get("height", 0),
                ).get("window_id")
    except (OSError, ValueError, SystemExit, ImportError, KeyError, TypeError):
        pass
    return None


def accessibility_hint() -> str:
    return (
        "无法读取浏览器窗口。请在 系统设置 → 隐私与安全性 → 辅助功能 中，"
        "给当前运行命令的 Terminal/iSelf/opencode 授权；Chrome 还需要允许通过 Apple Events 执行 JavaScript。"
    )


def screenshot(run: Path, browser: str, title: str, reason: str) -> Path:
    evidence = run / "evidence"
    evidence.mkdir(exist_ok=True)
    numbers = []
    for image in evidence.glob("evidence-*.png"):
        match = re.match(r"evidence-(\d+)-", image.name)
        if match:
            numbers.append(int(match.group(1)))
    number = max(numbers, default=0) + 1
    stem = f"evidence-{number:03d}-{datetime.now():%Y%m%d-%H%M%S}"
    image = evidence / f"{stem}.png"
    app = browser_name(browser)
    window_id = app_process_window_id(app)
    command = ["screencapture", "-x", "-o"]
    scope = "window"
    if window_id is not None:
        command += ["-l", str(window_id)]
    else:
        command += ["-m"]
        scope = "screen"
    command.append(str(image))
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"无法截图：{exc}") from exc
    if completed.returncode or not image.is_file():
        raise SystemExit(
            f"无法截图：{(completed.stderr or completed.stdout or '').strip()}\n{accessibility_hint()}"
        )
    info = page_info(browser)
    metadata = {
        "step": number,
        "title": title,
        "reason": reason,
        "image": image.name,
        "platform": "web",
        "browser": browser,
        "scope": scope,
        "window_id": window_id,
        "url": info["url"],
        "page_title": info["title"],
        "timestamp": now(),
    }
    write_json(evidence / f"{stem}.json", metadata)
    append_event(run, {"kind": "evidence", **metadata})
    print(image)
    return image


def require_run(value: str) -> Path:
    run = Path(value).expanduser().resolve()
    if not (run / "manifest.json").is_file():
        raise SystemExit(f"{run} 不是有效的网页走查 run 目录")
    return run


def new_run(title: str, task: str, browser: str, url: str | None) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    slug = "-".join(re.findall(r"[a-z0-9]+", title.lower()))[:50] or "web-walkthrough"
    run = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"
    (run / "evidence").mkdir(parents=True)
    manifest = {
        "title": title,
        "task": task,
        "platform": "web",
        "browser": browser,
        "url": url or "",
        "started_at": now(),
        "status": "running",
    }
    write_json(run / "manifest.json", manifest)
    write_json(run / "session.json", {"browser": browser})
    append_event(
        run,
        {
            "kind": "run_started",
            "platform": "web",
            "browser": browser,
            "url": url or "",
            "title": title,
            "task": task,
        },
    )
    return run


DOM_TEXT_JS = r"""(() => {
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const nodes = [...document.querySelectorAll('body *')].filter(visible);
  const elements = [];
  for (const el of nodes) {
    const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || '').replace(/\s+/g, ' ').trim();
    if (!text || text.length > 500) continue;
    const role = el.getAttribute('role') || el.tagName.toLowerCase();
    const interactive = /^(a|button|input|textarea|select|summary)$/i.test(el.tagName) || el.getAttribute('role') === 'button' || el.tabIndex >= 0;
    const childText = [...el.children].some(c => (c.innerText || '').trim() === text);
    if (childText && !interactive) continue;
    const r = el.getBoundingClientRect();
    elements.push({text, role, interactive, x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height)});
  }
  return JSON.stringify({url: location.href, title: document.title, dpr: devicePixelRatio, elements});
})()"""


def cmd_start(args: argparse.Namespace) -> None:
    if args.url:
        subprocess.run(
            ["open", "-a", browser_name(args.browser), args.url], check=False
        )
    else:
        subprocess.run(["open", "-a", browser_name(args.browser)], check=False)
    time.sleep(args.wait)
    try:
        info = page_info(args.browser)
    except SystemExit as exc:
        raise SystemExit(
            f"无法连接到 {browser_name(args.browser)} 的当前页面：{exc}"
        ) from exc
    run = new_run(args.title, args.task, args.browser, info["url"])
    print(run)


def cmd_status(_: argparse.Namespace) -> None:
    rows = []
    for browser in BROWSERS:
        try:
            info = page_info(browser)
            rows.append(
                {
                    "browser": browser,
                    **info,
                    "window_id": app_process_window_id(browser_name(browser)),
                }
            )
        except SystemExit as exc:
            rows.append({"browser": browser, "error": str(exc)})
    print(
        json.dumps({"platform": "web", "browsers": rows}, ensure_ascii=False, indent=2)
    )


def cmd_screenshot(args: argparse.Namespace) -> None:
    screenshot(require_run(args.run), args.browser, args.title, args.reason)


def cmd_texts(args: argparse.Namespace) -> None:
    run = require_run(args.run)
    browser = args.browser
    try:
        payload = browser_js(browser, DOM_TEXT_JS)
    except SystemExit as exc:
        raise SystemExit(f"无法读取网页 DOM：{exc}\n{accessibility_hint()}") from exc
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"网页返回了无效的 DOM JSON：{payload[:200]}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("网页没有返回可读 DOM")
    if args.query:
        needle = args.query.casefold()
        payload["elements"] = [
            e
            for e in payload.get("elements", [])
            if needle in e.get("text", "").casefold()
        ]
    write_json(run / "ui-latest.json", payload)
    append_event(
        run,
        {
            "kind": "ui_dump",
            "path": "ui-latest.json",
            "platform": "web",
            "browser": browser,
        },
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_tap_text(args: argparse.Namespace) -> None:
    lowered = args.text.casefold()
    if not args.allow_risky and any(word.casefold() in lowered for word in RISKY_WORDS):
        raise SystemExit(
            "拒绝点击疑似有副作用的网页控件；只有用户明确批准后才能加 --allow-risky"
        )
    run = require_run(args.run)
    exact = "true" if args.exact else "false"
    text = json.dumps(args.text, ensure_ascii=False)
    js = f"""(() => {{
      const q = {text}.toLocaleLowerCase();
      const nodes = [...document.querySelectorAll('a,button,input,textarea,select,[role="button"],[tabindex]')];
      const get = e => (e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('placeholder') || '').replace(/\\s+/g,' ').trim();
      const visible = e => {{ const r=e.getBoundingClientRect(),s=getComputedStyle(e); return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'; }};
      const matches = nodes.filter(e => visible(e)).map(e => [e,get(e)]).filter(([e,t]) => {exact} ? t.toLocaleLowerCase()===q : t.toLocaleLowerCase().includes(q));
      if (!matches.length) return JSON.stringify({{error:'当前页面没有匹配的可见控件'}});
      const index = {args.index if args.index is not None else 0};
      if (index < 0 || index >= matches.length) return JSON.stringify({{error:`匹配项索引超出范围：0..${{matches.length-1}}`}});
      const [el,label] = matches[index]; el.click();
      return JSON.stringify({{text:label,index,count:matches.length}});
    }})()"""
    result = browser_js(args.browser, js)
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict) or result.get("error"):
        raise SystemExit((result or {}).get("error", "网页点击失败"))
    append_event(
        run,
        {
            "kind": "action",
            "type": "tap",
            "target_text": result.get("text", args.text),
            "reason": args.reason,
            "platform": "web",
        },
    )
    print(json.dumps(result, ensure_ascii=False))


def cmd_scroll(args: argparse.Namespace) -> None:
    run = require_run(args.run)
    before = None
    if args.check:
        before = screenshot(run, args.browser, "滚动前", args.reason)
    direction = -1 if args.direction == "up" else 1
    js = f"window.scrollBy({{top: Math.round(window.innerHeight * 0.8 * {direction}), left: 0, behavior: 'instant'}}); JSON.stringify({{scrollY: window.scrollY}})"
    browser_js(args.browser, js)
    append_event(
        run,
        {
            "kind": "action",
            "type": "scroll",
            "direction": args.direction,
            "reason": args.reason,
            "platform": "web",
        },
    )
    time.sleep(args.wait)
    if before is not None:
        after = screenshot(run, args.browser, args.title, args.reason)
        difference = screen_change(before, after)
        changed = difference > args.threshold
        if not changed:
            for suffix in (".png", ".json"):
                after.with_suffix(suffix).unlink(missing_ok=True)
        append_event(
            run,
            {
                "kind": "scroll_check",
                "changed": changed,
                "difference": round(difference, 6),
                "threshold": args.threshold,
                "platform": "web",
            },
        )
        print(
            json.dumps(
                {
                    "changed": changed,
                    "difference": round(difference, 6),
                    "image": str(after) if changed else None,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(
            json.dumps(
                {"type": "scroll", "direction": args.direction}, ensure_ascii=False
            )
        )


def cmd_input(args: argparse.Namespace) -> None:
    lowered = args.text.casefold()
    if any(word.casefold() in lowered for word in RISKY_WORDS) or any(
        word in args.text for word in ("密码", "验证码", "身份证", "银行卡")
    ):
        raise SystemExit("拒绝输入疑似密码、验证码或支付信息")
    run = require_run(args.run)
    value = json.dumps(args.text, ensure_ascii=False)
    js = f"(() => {{ const e=document.activeElement; if(!e || !('value' in e)) return JSON.stringify({{error:'当前没有聚焦的输入框'}}); e.value={value}; e.dispatchEvent(new Event('input',{{bubbles:true}})); e.dispatchEvent(new Event('change',{{bubbles:true}})); return JSON.stringify({{ok:true}}); }})()"
    result = browser_js(args.browser, js)
    if isinstance(result, str):
        result = json.loads(result)
    if result.get("error"):
        raise SystemExit(result["error"])
    append_event(
        run,
        {
            "kind": "action",
            "type": "input_text",
            "text_length": len(args.text),
            "reason": args.reason,
            "platform": "web",
        },
    )
    print(json.dumps(result, ensure_ascii=False))


def cmd_close(args: argparse.Namespace) -> None:
    run = require_run(args.run)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(status="closed", closed_at=now())
    write_json(run / "manifest.json", manifest)
    append_event(run, {"kind": "run_closed", "platform": "web"})
    print(run)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="走查 Chrome/Safari 网页产品，每一步保留截图和 DOM 证据"
    )
    sub = cli.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="读取 Chrome/Safari 当前页面")
    status.set_defaults(func=cmd_status)
    start = sub.add_parser("start-run", help="打开网页并创建走查 run")
    start.add_argument("--browser", choices=BROWSERS, default="chrome")
    start.add_argument("--url")
    start.add_argument("--title", required=True)
    start.add_argument("--task", required=True)
    start.add_argument("--wait", type=float, default=2.0)
    start.set_defaults(func=cmd_start)
    for name, func, help_text in (
        ("screenshot", cmd_screenshot, "截取当前网页窗口"),
        ("texts", cmd_texts, "读取当前网页可见文字和控件"),
        ("tap-text", cmd_tap_text, "按网页可见文字点击"),
        ("scroll", cmd_scroll, "滚动网页"),
        ("input", cmd_input, "向当前聚焦输入框输入普通测试文字"),
        ("close", cmd_close, "结束 run，不关闭浏览器"),
    ):
        command = sub.add_parser(name, help=help_text)
        if name != "close":
            command.add_argument("--run", required=True)
            command.add_argument("--browser", choices=BROWSERS, required=True)
        else:
            command.add_argument("--run", required=True)
        if name == "screenshot":
            command.add_argument("--title", required=True)
            command.add_argument("--reason", required=True)
        elif name == "texts":
            command.add_argument("--query")
        elif name == "tap-text":
            command.add_argument("text")
            command.add_argument("--exact", action="store_true")
            command.add_argument("--index", type=int, default=0)
            command.add_argument("--allow-risky", action="store_true")
            command.add_argument("--reason", required=True)
        elif name == "scroll":
            command.add_argument("direction", choices=("down", "up"))
            command.add_argument("--check", action="store_true")
            command.add_argument("--title", default="滚动后网页")
            command.add_argument("--reason", required=True)
            command.add_argument("--wait", type=float, default=1.0)
            command.add_argument("--threshold", type=float, default=0.02)
        elif name == "input":
            command.add_argument("--text", required=True)
            command.add_argument("--reason", required=True)
        command.set_defaults(func=func)
    return cli


if __name__ == "__main__":
    args = parser().parse_args()
    args.func(args)
