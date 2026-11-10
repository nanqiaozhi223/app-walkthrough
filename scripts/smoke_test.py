#!/usr/bin/env python3
"""Offline smoke test: report pipeline (ux + collect), post parser and device helpers.

Does not touch a phone or Appium.
"""

from __future__ import annotations

import json
import struct
import zlib
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "scripts" / "report.py"


def tiny_png(width: int = 2, height: int = 2, rgb: tuple[int, int, int] = (200, 160, 90)) -> bytes:
    """A solid-colour PNG built from scratch, so the test needs no image files."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    scanline = b"\x00" + bytes(rgb) * width  # filter byte 0, then the pixels
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(scanline * height)) + chunk(b"IEND", b"")


def run_report(run: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPORT), "--run", str(run), *extra], capture_output=True, text=True)


def make_run(base: Path, name: str, posts: list[dict] | None = None) -> Path:
    run = base / name
    shots = run / "evidence"
    shots.mkdir(parents=True)
    manifest = {
        "title": name, "task": "离线测试", "bundle_id": "com.example.app",
        "app": {"name": "示例", "version": "1.0"}, "device": {"model": "iPhone 15 Plus", "os_version": "26.1"},
        "started_at": "2026-01-01T00:00:00Z",
    }
    (run / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    events = [
        {"timestamp": "2026-01-01T00:00:00Z", "kind": "run_started"},
        {"timestamp": "2026-01-01T00:00:05Z", "kind": "action", "type": "tap", "x": 10, "y": 20, "target_text": "热榜", "reason": "切换标签"},
        {"timestamp": "2026-01-01T00:00:06Z", "kind": "evidence", "image": "evidence-001.png"},
        {"timestamp": "2026-01-01T00:00:08Z", "kind": "action", "type": "scroll", "direction": "down", "reason": "看下一屏"},
        {"timestamp": "2026-01-01T00:00:09Z", "kind": "scroll_check", "changed": False},
    ]
    (shots / "evidence-001.json").write_text(json.dumps({"title": "热榜", "reason": "验证", "image": "evidence-001.png"}, ensure_ascii=False), encoding="utf-8")
    (shots / "evidence-001.png").write_bytes(tiny_png())
    if posts is not None:
        events.append({"timestamp": "2026-01-01T00:01:00Z", "kind": "action", "type": "collect", "label": "关键词 / 动态", "reason": "采集"})
        events.append({"timestamp": "2026-01-01T00:02:00Z", "kind": "collect_done", "label": "关键词 / 动态", "screens": 3, "total": len(posts), "reached_end": True,
                       "comments_per_post": 5, "opened": 1, "open_failed": 0, "not_opened": 1, "comments": 2})
        # U+2028 inside a body must not split the JSONL row.
        (run / "collected.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in posts), encoding="utf-8")
    (run / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    return run


def check_ux_report(base: Path) -> None:
    run = make_run(base, "ux")
    assert run_report(run).returncode != 0, "must refuse to render without analysis.json"

    draft = run_report(run, "--draft")
    assert draft.returncode == 0, draft.stderr
    assert "草稿" in (run / "report.md").read_text(encoding="utf-8")

    assert run_report(run, "--init").returncode == 0
    assert run_report(run, "--init").returncode != 0, "--init must not overwrite without --force"
    blocked = run_report(run)
    assert blocked.returncode == 2 and "TODO" in blocked.stderr, blocked.stderr

    analysis = {
        "mode": "ux", "status": "completed",
        "summary": "热榜入口清楚，切换后内容立即刷新。",
        "takeaways": ["热榜标签在顶部第四位，一次点击可达（图1）"],
        "modules": [{"name": "热榜", "purpose": "查看热门内容", "seen": "排行列表", "rating": "清晰", "figures": [1]}],
        "strengths": [{"title": "排行醒目", "detail": "序号用红色标出", "figures": [1]}],
        "issues": [{"severity": "Low", "title": "示例问题", "evidence": "示例", "impact": "示例", "suggestion": "示例", "figures": [9]}],
        "not_tested": ["未点开帖子"],
    }
    (run / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    bad = run_report(run)
    assert bad.returncode == 2 and "图9" in bad.stderr, bad.stderr

    analysis["issues"][0]["figures"] = [1]
    (run / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    ok = run_report(run)
    assert ok.returncode == 0, ok.stderr
    assert "结论：热榜入口清楚" in ok.stdout
    md = (run / "report.md").read_text(encoding="utf-8")
    for expected in ("## 1. 结论", "热榜入口清楚", "点击「热榜」", "页面无变化", "示例 1.0（com.example.app）", "iPhone 15 Plus / iOS 26.1"):
        assert expected in md, expected
    assert "(10, 20)" not in md, "raw coordinates must not appear in the report"
    html = (run / "report.html").read_text(encoding="utf-8")
    assert "data:image/" in html and "evidence/" not in html, "HTML must embed screenshots"


def check_collect_report(base: Path) -> None:
    posts = [
        {"id": "p001", "author": "甲", "date": "3天前", "body": "宽带被运营商认定跑PCDN后限速\u2028现在要签字才能解除", "labels": [], "counts": ["2", "8"],
         "comment_status": "ok", "comment_total": 8, "comments": [
             {"id": "p001-c01", "author": "评论者A", "date": "昨天", "body": "我也被限速了，打客服说要上门检查", "likes": "5", "labels": [], "replies": [], "reply_total": None},
             {"id": "p001-c02", "author": "评论者B", "date": "前天", "body": "换运营商吧", "likes": None, "labels": [], "replies": [], "reply_total": None}]},
        {"id": "p002", "author": "乙", "date": "5月1日", "body": "出机顶盒，不跑PCDN，适合老人使用，联系我", "labels": [], "counts": [], "comment_status": "not_opened"},
        {"id": "p003", "author": "丙", "date": None, "body": "这条帖子没有被归类，应该出现在附录里，内容足够长", "labels": [], "counts": []},
    ]
    run = make_run(base, "collect", posts)
    init = run_report(run, "--init")
    assert init.returncode == 0 and "collect" in init.stdout, init.stdout

    analysis = {
        "mode": "collect", "status": "partial", "question": "用户遇到了哪些问题",
        "keywords": ["PCDN"],
        "summary": "3 条里 1 条反映运营商限速。",
        "takeaways": ["运营商限速（1 条）"],
        "themes": [{"name": "运营商限速", "description": "被限速", "items": ["p001", "p404"], "notes": {"p001": "要签字解除"}, "comments": ["p001-c01", "p001-c99"]}],
        "excluded": [{"reason": "二手广告", "items": ["p002"]}],
        "limitations": ["只看了 3 屏"], "next_steps": ["换排序再采"],
    }
    (run / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    bad = run_report(run)
    assert bad.returncode == 2 and "p404" in bad.stderr and "p001-c99" in bad.stderr, bad.stderr

    analysis["themes"][0]["items"] = ["p001"]
    analysis["themes"][0]["comments"] = ["p001-c01"]
    (run / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    ok = run_report(run)
    assert ok.returncode == 0, ok.stderr
    assert "p003" in ok.stderr, "unclassified posts must be reported"
    md = (run / "report.md").read_text(encoding="utf-8")
    for expected in (
        "问题：用户遇到了哪些问题", "共采集 3 条，归入主题 1 条，排除 1 条，未归类 1 条", "| 运营商限速 | 1 | 33% |",
        "要签字解除", "附：未归类帖子", "已到列表底部",
        "评论：打开 1 条帖子，每条最多取默认排序的前 5 条评论，共 2 条；1 条没来得及打开",
        "| 2 / 8 |", "| p001-c01 | p001 | 评论者A | 我也被限速了",
    ):
        assert expected in md, expected
    assert "评论 {comments}" not in init.stdout and "评论 2 条" in init.stdout, init.stdout


SOURCE_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<AppiumAUT><XCUIElementTypeApplication name="示例" label="示例" visible="true" x="0" y="0" width="430" height="932">
  <XCUIElementTypeNavigationBar name="Demo.MainView" visible="true" x="0" y="59" width="430" height="54"/>
  <XCUIElementTypeImage name="ic share outline white 24dp" visible="true" x="10" y="60" width="24" height="24"/>
  <XCUIElementTypeImage name="AdditionalDimmingOverlay" visible="true" x="0" y="0" width="430" height="932"/>
  <XCUIElementTypeOther name="热榜" label="热榜" visible="true" x="180" y="110" width="60" height="40">
    <XCUIElementTypeStaticText value="热榜" name="热榜" label="热榜" visible="true" x="186" y="116" width="34" height="24"/>
  </XCUIElementTypeOther>
  <XCUIElementTypeStaticText value="热榜" label="热榜" visible="true" x="20" y="800" width="34" height="24"/>
  <XCUIElementTypeStaticText value="隐藏文字" label="隐藏文字" visible="false" x="0" y="0" width="0" height="0"/>
</XCUIElementTypeApplication></AppiumAUT>"""

FEED_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<AppiumAUT><XCUIElementTypeCollectionView visible="true" x="0" y="0" width="430" height="932">
  <XCUIElementTypeCell visible="true" x="0" y="0" width="430" height="932">
    <XCUIElementTypeCell visible="true" x="0" y="200" width="430" height="600">
      <XCUIElementTypeButton name="ic keyboard arrow down" visible="true"/>
      <XCUIElementTypeOther label="作者甲" visible="true"/>
      <XCUIElementTypeOther label="8天前" visible="true"/>
      <XCUIElementTypeOther label="iPhone 15" visible="true"/>
      <XCUIElementTypeOther label="#话题# 这是一条足够长的帖子正文，用来测试解析是否正确识别" visible="true" x="20" y="257" width="390" height="126"/>
      <XCUIElementTypeStaticText label="话题" visible="true"/>
      <XCUIElementTypeButton label="6" visible="true"><XCUIElementTypeStaticText label="6" visible="true"/></XCUIElementTypeButton>
      <XCUIElementTypeButton label="44" visible="true"><XCUIElementTypeStaticText label="44" visible="true"/></XCUIElementTypeButton>
    </XCUIElementTypeCell>
  </XCUIElementTypeCell>
  <XCUIElementTypeCell visible="true" x="0" y="100" width="100" height="40"><XCUIElementTypeStaticText label="综合" visible="true"/></XCUIElementTypeCell>
</XCUIElementTypeCollectionView></AppiumAUT>"""

AD_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<AppiumAUT><XCUIElementTypeCell visible="true" x="0" y="200" width="430" height="300">
  <XCUIElementTypeStaticText label="今日推荐" visible="true"/>
  <XCUIElementTypeOther label="免切免洗免调味，三步做好一道招牌菜，快来试试" visible="true"/>
  <XCUIElementTypeStaticText label="下载安装" visible="true"/>
  <XCUIElementTypeButton label="广告" visible="true"/>
</XCUIElementTypeCell></AppiumAUT>"""

# A post page whose body is drawn without accessible text; only the composer marks it as a post.
DETAIL_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<AppiumAUT><XCUIElementTypeApplication label="示例" visible="true" x="0" y="0" width="430" height="932">
  <XCUIElementTypeNavigationBar label="动态" visible="true" x="0" y="59" width="430" height="54">
    <XCUIElementTypeButton name="navigation back 24" visible="true" x="24" y="63" width="36" height="36"/>
  </XCUIElementTypeNavigationBar>
  <XCUIElementTypeCell visible="true" x="0" y="-5" width="430" height="400">
    <XCUIElementTypeOther label="楼主X" visible="true"/>
    <XCUIElementTypeOther label="可以看直播了" visible="true"/>
    <XCUIElementTypeStaticText label="楼主回复了" visible="true"/>
    <XCUIElementTypeOther label="16小时前" visible="true"/>
    <XCUIElementTypeButton label="1" visible="true"><XCUIElementTypeStaticText label="1" visible="true"/></XCUIElementTypeButton>
    <XCUIElementTypeOther label="楼主X：流畅度没问题" visible="true"/>
  </XCUIElementTypeCell>
  <XCUIElementTypeCell visible="true" x="0" y="125" width="60" height="28"><XCUIElementTypeStaticText label="最新" visible="true"/></XCUIElementTypeCell>
  <XCUIElementTypeCell visible="true" x="0" y="400" width="430" height="156">
    <XCUIElementTypeOther label="路人Y" visible="true"/>
    <XCUIElementTypeOther label="今天运营商打电话说我跑 PCDN" visible="true"/>
    <XCUIElementTypeOther label="昨天" visible="true"/>
    <XCUIElementTypeOther label="查看全部 12 条回复" visible="true"/>
  </XCUIElementTypeCell>
  <XCUIElementTypeCell visible="true" x="0" y="560" width="430" height="300">
    <XCUIElementTypeStaticText label="今日推荐" visible="true"/>
    <XCUIElementTypeOther label="广告主" visible="true"/>
    <XCUIElementTypeOther label="前天" visible="true"/>
    <XCUIElementTypeButton label="广告" visible="true"/>
  </XCUIElementTypeCell>
  <XCUIElementTypeCell visible="true" x="0" y="860" width="430" height="115">
    <XCUIElementTypeOther label="路人Z" visible="true"/>
    <XCUIElementTypeOther label="能上车了吗" visible="true"/>
    <XCUIElementTypeOther label="前天" visible="true"/>
  </XCUIElementTypeCell>
  <XCUIElementTypeImage name="bottomBar_writeComment" visible="true" x="20" y="869" width="200" height="40"/>
</XCUIElementTypeApplication></AppiumAUT>"""


def check_device_helpers() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import device  # noqa: E402

    help_text = subprocess.run([sys.executable, str(ROOT / "scripts" / "device.py"), "--help"], check=True, capture_output=True, text=True).stdout
    for command in ("start-run", "screenshot", "texts", "tap-text", "scroll", "collect", "close"):
        assert command in help_text, command

    tree = device.ET.fromstring(SOURCE_FIXTURE)
    texts = [item["text"] for item in device.page_texts(tree)]
    assert "热榜" in texts and "示例" in texts, texts
    for noise in ("Demo.MainView", "ic share outline white 24dp", "AdditionalDimmingOverlay", "隐藏文字"):
        assert noise not in texts, noise
    distinct = device.innermost([item for item in device.page_texts(tree, dedupe=False) if item["text"] == "热榜"])
    assert len(distinct) == 2 and {item["type"] for item in distinct} == {"StaticText"}, distinct
    assert any(word in "立即购买" for word in device.RISKY_WORDS)
    assert not any(word in "热榜" for word in device.RISKY_WORDS)

    posts = device.parse_posts(device.ET.fromstring(FEED_FIXTURE))
    assert len(posts) == 1, posts
    post = posts[0]
    assert post["author"] == "作者甲" and post["date"] == "8天前", post
    assert post["counts"] == ["6", "44"], post
    assert "iPhone 15" in post["labels"] and "话题" in post["labels"], post

    partial = {"author": None, "date": None, "body": post["body"], "labels": [], "counts": [], "extra": []}
    device.merge_post(partial, post)
    assert partial["author"] == "作者甲" and partial["counts"] == ["6", "44"], partial

    apps = [{"bundleIdentifier": "com.name.WebDriverAgentRunner.xctrunner"}, {"bundleIdentifier": "com.facebook.WebDriverAgentRunner.xctrunner"}]
    assert device.detect_wda_bundle_id(apps) == "com.name.WebDriverAgentRunner"
    assert device.detect_wda_bundle_id(apps + [{"bundleIdentifier": "com.x.WebDriverAgentRunner.xctrunner"}]) is None
    assert device.app_info(apps, "com.missing") is None and device.app_info(None, "com.any") == {}

    # Feed posts keep a tap target; the tap point stays inside the visible part of the body.
    assert post["_frame"] == {"x": 20, "y": 257, "width": 390, "height": 126}, post
    assert device.tap_point(post["_frame"], 932, 200, 100) == (215, 320)
    assert device.tap_point({"x": 0, "y": 820, "width": 400, "height": 200}, 932, 200, 100) is None, "body hidden behind bottom bar"
    assert device.saved(post).keys() == post.keys() - {"_frame"}
    assert device.parse_posts(device.ET.fromstring(AD_FIXTURE)) == [], "sponsored cards are not posts"

    detail = device.ET.fromstring(DETAIL_FIXTURE)
    assert device.back_button(detail) == (42, 81)
    assert device.back_button(tree) is None
    assert device.is_post_detail(detail, "帖子正文在这个 App 里画成图片，读不到文字")
    assert not device.is_post_detail(tree, "帖子正文在这个 App 里画成图片，读不到文字")
    comments = device.parse_comments(detail, "#话题# 这是一条足够长的帖子正文，用来测试解析是否正确识别")
    assert [c["author"] for c in comments] == ["楼主X", "路人Y", "路人Z"], comments
    first, second, third = comments
    assert first["body"] == "可以看直播了" and first["labels"] == ["楼主回复了"] and first["likes"] == "1", first
    assert first["replies"] == ["楼主X：流畅度没问题"] and first["reply_total"] == 1, first
    assert second["body"].startswith("今天运营商打电话") and second["date"] == "昨天" and second["reply_total"] == 12, second
    assert third["likes"] is None and third["replies"] == [], third

    # Drags must not start on a sponsored cell (one opened WeChat during testing).
    assert device.ad_bands(detail) == [(560, 860)], device.ad_bands(detail)
    start = device.drag_start(detail, 932, 372)
    assert start is not None and not 550 <= start <= 870 and start - 372 >= int(932 * 0.18), start
    assert device.drag_start(tree, 932, 372) == int(932 * 0.75), "no ads: start low on the screen"
    full_ad = device.ET.fromstring('<AppiumAUT><XCUIElementTypeCell x="0" y="0" width="430" height="932"><XCUIElementTypeButton label="广告"/></XCUIElementTypeCell></AppiumAUT>')
    assert device.drag_start(full_ad, 932, 372) is None, "ad everywhere: do not drag"


def check_screen_compare(base: Path) -> None:
    """png_size reads the header; screen_change tells identical shots from different ones."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import device  # noqa: E402

    light, dark = base / "light.png", base / "dark.png"
    light.write_bytes(tiny_png(30, 60, (240, 240, 240)))
    dark.write_bytes(tiny_png(30, 60, (20, 20, 20)))
    assert device.png_size(light.read_bytes()) == (30, 60)
    assert device.png_size(b"not a png") == (None, None)
    assert device.screen_change(light, light) == 0.0
    assert device.screen_change(light, dark) > 0.5, device.screen_change(light, dark)

    run = base / "shots"
    (run / "evidence").mkdir(parents=True)
    for name in ("evidence-009-20260101-000000.png", "evidence-010-20260101-000001.png"):
        (run / "evidence" / name).write_bytes(tiny_png())
    assert device.latest_screenshot(run).name.startswith("evidence-010"), "numeric order, not text order"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="app-walkthrough-smoke-") as temp:
        base = Path(temp)
        check_ux_report(base)
        check_collect_report(base)
        check_screen_compare(base)
    check_device_helpers()
    print("smoke test passed")


if __name__ == "__main__":
    main()
