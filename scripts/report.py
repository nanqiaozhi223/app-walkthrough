#!/usr/bin/env python3
"""Render an App Experience Agent run as Markdown and standalone HTML.

Workflow:
  1. report.py --run RUN_DIR --init     writes an analysis.json skeleton
  2. Codex fills every TODO in analysis.json
  3. report.py --run RUN_DIR            validates it and renders report.md + report.html

Two modes:
  ux       UI/UX walkthrough: conclusion, functional modules, strengths, issues.
  collect  Content collection: themes over RUN_DIR/collected.jsonl (from device.py collect).

--draft renders only the journey and screenshots, for checking progress mid-run.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except ImportError:  # Pillow is optional; sips or the original image is used instead.
    Image = None  # type: ignore[assignment]


STATUS = {"completed": "已完成", "partial": "部分完成", "blocked": "受阻"}
RATINGS = ("清晰", "一般", "有问题")
SEVERITY = ("High", "Medium", "Low")
SEVERITY_LABEL = {"High": "高", "Medium": "中", "Low": "低"}
TODO = "TODO"


# ---------------------------------------------------------------- run data


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    # JSONL rows end at \n only; splitlines() would also break on U+2028 inside a post.
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def figures(run: Path) -> list[dict[str, Any]]:
    """Evidence screenshots that still exist, numbered 1..N in capture order."""
    shots = []
    for meta_path in sorted((run / "evidence").glob("*.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (run / "evidence" / str(meta.get("image", ""))).is_file():
            shots.append(meta)
    for number, shot in enumerate(shots, 1):
        shot["figure"] = number
    return shots


def local_time(stamp: str | None) -> str:
    if not stamp:
        return "未知"
    try:
        moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def duration(start: str | None, end: str | None) -> str:
    try:
        first = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = dt.datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return "未知"
    minutes = max(0, int((last - first).total_seconds() // 60))
    return f"约 {minutes} 分钟" if minutes else "不到 1 分钟"


def describe(event: dict[str, Any]) -> str:
    kind = event.get("type")
    if kind == "tap":
        # iOS labels can carry a second line such as "Tab 2 of 4"; show only the visible label.
        target = str(event.get("target_text") or "").split("\n")[0].strip()
        return f"点击「{target}」" if target else "点击截图中确认的位置"
    if kind == "scroll":
        return {"down": "向下滚动", "up": "向上滚动", "left": "向左滑动", "right": "向右滑动"}.get(event.get("direction"), "滚动")
    if kind == "swipe":
        return "滑动"
    if kind == "input_text":
        return f"输入 {event.get('text_length', 0)} 个字（内容不记录）"
    if kind == "key":
        return {"ENTER": "确认 / 提交", "HOME": "回到主屏幕"}.get(event.get("key"), str(event.get("key")))
    if kind == "launch":
        return "切回目标 App"
    if kind == "wait":
        return f"等待 {event.get('seconds')} 秒"
    if kind == "collect":
        return f"采集列表「{event.get('label', '')}」"
    return str(kind or "操作")


def timeline(events: list[dict[str, Any]], shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per user-visible step, with the screenshots captured right after it."""
    by_image = {shot["image"]: shot["figure"] for shot in shots}
    steps: list[dict[str, Any]] = [{"action": "开始", "reason": "进入目标 App", "figures": []}]
    for event in events:
        kind = event.get("kind")
        if kind == "action":
            step = {"action": describe(event), "reason": event.get("reason", ""), "figures": []}
            if event.get("type") == "collect" and "total" in event:  # runs recorded before collect_done existed
                step["action"] += f"：滚动 {event.get('screens')} 屏，累计 {event.get('total')} 条"
            steps.append(step)
        elif kind == "evidence" and event.get("image") in by_image:
            steps[-1]["figures"].append(by_image[event["image"]])
        elif kind == "scroll_check" and event.get("changed") is False:
            steps[-1]["action"] += "（页面无变化，已到底部或未滚动）"
        elif kind == "left_app":
            steps[-1]["action"] += f"（中途跳到了 {event.get('foreground')}，已切回并停止）"
        elif kind == "collect_done":
            ending = "已到列表底部" if event.get("reached_end") else "达到屏数上限"
            steps[-1]["action"] += f"：滚动 {event.get('screens')} 屏，共 {event.get('total')} 条，{ending}"
    return [step for step in steps if step["action"] != "开始" or step["figures"]]


def collect_scope(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per collect pass, including passes recorded before collect_done existed."""
    return [
        event for event in events
        if event.get("kind") == "collect_done" or (event.get("kind") == "action" and event.get("type") == "collect" and "total" in event)
    ]


# ---------------------------------------------------------------- analysis.json


def skeleton(mode: str, manifest: dict[str, Any]) -> dict[str, Any]:
    common = {
        "mode": mode,
        "status": "TODO: completed / partial / blocked",
        "summary": "TODO: 一两句话直接回答用户的问题",
        "takeaways": ["TODO: 要点 1（写具体对象和数量，不写空话）", "TODO: 要点 2", "TODO: 要点 3"],
        "limitations": ["TODO: 本次没看到或无法确认的内容"],
        "next_steps": ["TODO: 能安全扩大覆盖范围的最小下一步"],
    }
    if mode == "ux":
        return {
            **common,
            "modules": [{"name": "TODO", "purpose": "TODO", "seen": "TODO: 实际看到了什么", "rating": "TODO: 清晰 / 一般 / 有问题", "figures": [1]}],
            "strengths": [{"title": "TODO", "detail": "TODO", "figures": [1]}],
            "issues": [{"severity": "TODO: High / Medium / Low", "title": "TODO", "evidence": "TODO: 截图里能看到什么", "impact": "TODO", "suggestion": "TODO", "figures": [1]}],
            "not_tested": ["TODO: 未访问的模块、刻意没点的购买/领取/发布等操作"],
        }
    return {
        **common,
        "question": f"TODO: 用户关心的问题，例如“{manifest.get('task', '')}”",
        "keywords": ["TODO: 采集关键词，用于摘要定位，例如 PCDN"],
        "themes": [{"name": "TODO", "description": "TODO: 这一类帖子在说什么", "items": ["p001"], "notes": {"p001": "TODO: 可选，一句话说清这条帖子的要点；不需要就删掉 notes"}, "comments": ["TODO: 可选，引用最能说明问题的评论编号，如 p001-c02；没有采集评论就删掉"]}],
        "excluded": [{"reason": "TODO: 例如“只是二手交易广告，与体验无关”", "items": []}],
    }


def find_todo(value: Any, path: str = "") -> list[str]:
    if isinstance(value, str):
        return [path or "(root)"] if TODO in value else []
    if isinstance(value, list):
        return [hit for index, item in enumerate(value) for hit in find_todo(item, f"{path}[{index}]")]
    if isinstance(value, dict):
        return [hit for key, item in value.items() for hit in find_todo(item, f"{path}.{key}" if path else key)]
    return []


def validate(analysis: dict[str, Any], shots: list[dict[str, Any]], items: dict[str, dict[str, Any]]) -> list[str]:
    errors = [f"{where} 还是 TODO" for where in find_todo(analysis)]
    mode = analysis.get("mode")
    if mode not in ("ux", "collect"):
        return errors + ["mode 必须是 ux 或 collect"]
    if analysis.get("status") not in STATUS:
        errors.append("status 必须是 completed / partial / blocked")
    if not str(analysis.get("summary", "")).strip():
        errors.append("summary 不能为空")
    takeaways = analysis.get("takeaways")
    if not isinstance(takeaways, list) or not 1 <= len(takeaways) <= 5:
        errors.append("takeaways 需要 1–5 条")

    figure_count = len(shots)

    def check_figures(where: str, refs: Any, required: bool) -> None:
        refs = refs or []
        if required and not refs:
            errors.append(f"{where}.figures 至少引用一张截图")
        for ref in refs:
            if not isinstance(ref, int) or not 1 <= ref <= figure_count:
                errors.append(f"{where}.figures 引用了不存在的图{ref}（共 {figure_count} 张）")

    if mode == "ux":
        modules = analysis.get("modules") or []
        if not modules:
            errors.append("modules 至少写一个功能模块")
        for index, module in enumerate(modules):
            where = f"modules[{index}]"
            for field in ("name", "purpose", "seen"):
                if not str(module.get(field, "")).strip():
                    errors.append(f"{where}.{field} 不能为空")
            if module.get("rating") not in RATINGS:
                errors.append(f"{where}.rating 必须是 {' / '.join(RATINGS)}")
            check_figures(where, module.get("figures"), required=False)
        for index, item in enumerate(analysis.get("strengths") or []):
            if not str(item.get("title", "")).strip():
                errors.append(f"strengths[{index}].title 不能为空")
            check_figures(f"strengths[{index}]", item.get("figures"), required=True)
        for index, item in enumerate(analysis.get("issues") or []):
            where = f"issues[{index}]"
            if item.get("severity") not in SEVERITY:
                errors.append(f"{where}.severity 必须是 High / Medium / Low")
            for field in ("title", "evidence", "impact", "suggestion"):
                if not str(item.get(field, "")).strip():
                    errors.append(f"{where}.{field} 不能为空")
            check_figures(where, item.get("figures"), required=True)
    else:
        if not items:
            errors.append("collect 模式需要 collected.jsonl，请先运行 device.py collect")
        if not str(analysis.get("question", "")).strip():
            errors.append("question 不能为空")
        themes = analysis.get("themes") or []
        if not themes:
            errors.append("themes 至少写一个主题")
        for index, theme in enumerate(themes):
            where = f"themes[{index}]"
            if not str(theme.get("name", "")).strip():
                errors.append(f"{where}.name 不能为空")
            if not theme.get("items"):
                errors.append(f"{where}.items 至少包含一条帖子")
            for ref in theme.get("items") or []:
                if ref not in items:
                    errors.append(f"{where}.items 里的 {ref} 不在 collected.jsonl 中")
            for ref in theme.get("notes") or {}:
                if ref not in (theme.get("items") or []):
                    errors.append(f"{where}.notes 里的 {ref} 不在这个主题的 items 中")
            for ref in theme.get("comments") or []:
                if ref not in comment_index(items):
                    errors.append(f"{where}.comments 里的 {ref} 不在 collected.jsonl 的评论中")
        for index, group in enumerate(analysis.get("excluded") or []):
            for ref in group.get("items") or []:
                if ref not in items:
                    errors.append(f"excluded[{index}].items 里的 {ref} 不在 collected.jsonl 中")
    return errors


# ---------------------------------------------------------------- document model
# A report is a list of blocks rendered twice, so Markdown and HTML never drift.


def refs(numbers: list[int] | None) -> str:
    return "、".join(f"图{n}" for n in numbers or []) or "—"


def comment_index(items: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {comment["id"]: comment for item in items.values() for comment in item.get("comments") or [] if comment.get("id")}


def comment_cell(item: dict[str, Any]) -> str:
    status = item.get("comment_status")
    if status is None:
        return "—"
    if status != "ok":
        return {"not_opened": "未打开", "wrong_page": "打开失败", "no_detail": "打开失败", "left_app": "跳到其他 App"}.get(status, status)
    got = len(item.get("comments") or [])
    total = item.get("comment_total")
    return f"{got} / {total}" if total is not None else str(got)


def excerpt(text: str, keywords: list[str] | None = None, limit: int = 70) -> str:
    """Shorten a post, centred on the first keyword hit so the relevant part stays visible."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    lowered = flat.lower()
    hits = [lowered.find(word.lower()) for word in keywords or [] if word and word.lower() in lowered]
    start = 0
    if hits and min(hits) > limit - 20:
        start = max(0, min(hits) - limit // 3)
    piece = flat[start:start + limit]
    return ("…" if start else "") + piece + ("…" if start + limit < len(flat) else "")


def header_blocks(manifest: dict[str, Any], kind: str, status: str | None, events: list[dict[str, Any]]) -> list[tuple]:
    app = manifest.get("app") or {}
    device = manifest.get("device") or {}
    app_text = " ".join(part for part in (app.get("name"), app.get("version")) if part)
    if manifest.get("bundle_id"):
        app_text = f"{app_text}（{manifest['bundle_id']}）" if app_text else manifest["bundle_id"]
    app_text = app_text or "未知"
    device_text = " / ".join(part for part in (device.get("model"), device.get("os_version") and f"iOS {device['os_version']}") if part) or "未知"
    end = manifest.get("closed_at") or (events[-1]["timestamp"] if events else None)
    meta = [
        ("任务", manifest.get("task") or "（未填写）"),
        ("App", app_text),
        ("设备", device_text),
        ("时间", f"{local_time(manifest.get('started_at'))}，{duration(manifest.get('started_at'), end)}"),
    ]
    if status:
        meta.insert(0, ("结果", STATUS[status]))
    return [("h1", f"{kind}：{manifest.get('title', '未命名')}"), ("meta", meta)]


def journey_blocks(steps: list[dict[str, Any]], shots: list[dict[str, Any]], number: int) -> list[tuple]:
    rows = [[str(index), step["action"], step["reason"], refs(step["figures"])] for index, step in enumerate(steps, 1)]
    blocks: list[tuple] = [("h2", f"{number}. 操作过程")]
    blocks.append(("table", ["步骤", "操作", "目的", "截图"], rows) if rows else ("p", "没有操作记录。"))
    blocks.append(("h2", f"{number + 1}. 截图"))
    blocks.append(("figures", shots) if shots else ("p", "没有截图。"))
    return blocks


def bullet_section(title: str, values: list[str] | None) -> list[tuple]:
    return [("h3", title), ("ul", values)] if values else []


def ux_blocks(manifest: dict[str, Any], analysis: dict[str, Any], steps: list[dict[str, Any]], shots: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[tuple]:
    blocks = header_blocks(manifest, "App 体验走查报告", analysis["status"], events)
    blocks += [("h2", "1. 结论"), ("p", analysis["summary"]), ("ul", analysis["takeaways"])]
    blocks += [("h2", "2. 功能模块"), ("table", ["模块", "作用", "实际看到", "体验", "截图"], [
        [m["name"], m["purpose"], m["seen"], m["rating"], refs(m.get("figures"))] for m in analysis["modules"]
    ])]
    strengths = analysis.get("strengths") or []
    blocks.append(("h2", "3. 做得好的地方"))
    if strengths:
        lines = []
        for item in strengths:
            detail = f"：{item['detail']}" if item.get("detail") else ""
            lines.append(f"{item['title']}{detail}（{refs(item.get('figures'))}）")
        blocks.append(("ul", lines))
    else:
        blocks.append(("p", "本次没有记录突出的优点。"))
    issues = sorted(analysis.get("issues") or [], key=lambda item: SEVERITY.index(item["severity"]))
    blocks.append(("h2", "4. 问题与建议"))
    if issues:
        blocks.append(("table", ["优先级", "问题", "证据", "影响", "建议", "截图"], [
            [SEVERITY_LABEL[i["severity"]], i["title"], i["evidence"], i["impact"], i["suggestion"], refs(i.get("figures"))] for i in issues
        ]))
    else:
        blocks.append(("p", "本次没有发现有截图佐证的问题。"))
    blocks.append(("h2", "5. 覆盖范围"))
    blocks += bullet_section("未验证 / 刻意未操作", analysis.get("not_tested"))
    blocks += bullet_section("局限", analysis.get("limitations"))
    blocks += bullet_section("建议的下一步", analysis.get("next_steps"))
    return blocks + journey_blocks(steps, shots, 6)


def collect_blocks(manifest: dict[str, Any], analysis: dict[str, Any], items: dict[str, dict[str, Any]], steps: list[dict[str, Any]], shots: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[tuple]:
    themes = sorted(analysis["themes"], key=lambda theme: -len(theme["items"]))
    themed = {ref for theme in themes for ref in theme["items"]}
    excluded = {ref for group in analysis.get("excluded") or [] for ref in group.get("items") or []}
    unclassified = [ref for ref in items if ref not in themed and ref not in excluded]
    total = len(items)

    blocks = header_blocks(manifest, "内容采集报告", analysis["status"], events)
    blocks += [("h2", "1. 结论"), ("p", f"问题：{analysis['question']}"), ("p", analysis["summary"]), ("ul", analysis["takeaways"])]

    scope = []
    for done in collect_scope(events):
        ending = "已到列表底部" if done.get("reached_end") else "达到设定的屏数上限，后面还有内容"
        scope.append(f"{done.get('label')}：滚动 {done.get('screens')} 屏，{ending}")
        if done.get("comments_per_post"):
            missed = []
            if done.get("open_failed"):
                missed.append(f"{done['open_failed']} 条打开失败")
            if done.get("not_opened"):
                missed.append(f"{done['not_opened']} 条没来得及打开")
            scope.append(
                f"评论：打开 {done.get('opened')} 条帖子，每条最多取默认排序的前 {done['comments_per_post']} 条评论，共 {done.get('comments')} 条"
                + ("；" + "，".join(missed) if missed else "")
            )
    scope.append(f"共采集 {total} 条，归入主题 {len(themed)} 条，排除 {len(excluded)} 条，未归类 {len(unclassified)} 条")
    scope.append(f"采集时间 {local_time(manifest.get('started_at'))}；帖子里的“N 天前”“M 月 D 日”都相对这个时间，App 通常不显示年份")
    blocks += [("h2", "2. 采集范围"), ("ul", scope)]

    blocks += [("h2", "3. 主题分布"), ("table", ["主题", "帖子数", "占比", "说明"], [
        [t["name"], str(len(t["items"])), f"{round(len(t['items']) * 100 / max(total, 1))}%", t.get("description", "")] for t in themes
    ]), ("note", "一条帖子可以同时属于多个主题，所以各主题数量相加可能超过总数。占比以全部采集条数为分母。")]

    blocks.append(("h2", "4. 主题详情"))
    keywords = analysis.get("keywords") or []
    has_comments = any("comment_status" in item for item in items.values())
    all_comments = comment_index(items)
    for theme in themes:
        notes = theme.get("notes") or {}
        head = ["编号", "发布者", "日期"] + (["要点"] if notes else []) + ["内容摘要", "互动数"] + (["评论"] if has_comments else [])
        rows = []
        for ref in theme["items"]:
            item = items[ref]
            row = [ref, item.get("author") or "—", item.get("date") or "—"]
            if notes:
                row.append(notes.get(ref, "—"))
            row += [excerpt(item.get("full_body") or item["body"], keywords), " / ".join(item.get("counts") or []) or "—"]
            if has_comments:
                row.append(comment_cell(item))
            rows.append(row)
        blocks += [("h3", f"{theme['name']}（{len(theme['items'])} 条）"), ("p", theme.get("description", "")), ("table", head, rows)]
        quoted = [ref for ref in theme.get("comments") or [] if ref in all_comments]
        if quoted:
            blocks.append(("table", ["评论", "所属帖子", "评论者", "内容", "点赞"], [
                [ref, ref.split("-")[0], all_comments[ref].get("author") or "—", excerpt(all_comments[ref]["body"], keywords, 90), all_comments[ref].get("likes") or "—"]
                for ref in quoted
            ]))
    note = "发布者和互动数由页面结构推断。互动数按 App 显示顺序排列（酷安为点赞 / 评论），不同 App 含义可能不同。"
    if has_comments:
        note += "「评论」列为“已采集 / 页面显示总数”，总数通常包含楼中楼回复。"
    blocks.append(("note", note))

    section = 5
    if excluded:
        blocks.append(("h2", f"{section}. 排除的内容"))
        blocks.append(("ul", [
            f"{group['reason']}：{len(group['items'])} 条（{'、'.join(group['items'])}）"
            for group in analysis.get("excluded") or [] if group.get("items")
        ]))
        section += 1
    blocks.append(("h2", f"{section}. 局限与下一步"))
    blocks += bullet_section("局限", analysis.get("limitations"))
    blocks += bullet_section("建议的下一步", analysis.get("next_steps"))
    if unclassified:
        blocks += [("h2", "附：未归类帖子"), ("table", ["编号", "发布者", "内容摘要"], [
            [ref, items[ref].get("author") or "—", excerpt(items[ref]["body"], analysis.get("keywords"))] for ref in unclassified
        ])]
    return blocks + journey_blocks(steps, shots, section + 1)


def draft_blocks(manifest: dict[str, Any], steps: list[dict[str, Any]], shots: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[tuple]:
    blocks = header_blocks(manifest, "走查记录（草稿）", None, events)
    blocks.append(("note", "这是草稿：只有操作过程和截图，还没有结论。运行 report.py --init 生成 analysis.json，填写后再生成正式报告。"))
    return blocks + journey_blocks(steps, shots, 1)


# ---------------------------------------------------------------- renderers


def md_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def to_markdown(blocks: list[tuple]) -> str:
    out: list[str] = []
    for block in blocks:
        kind = block[0]
        if kind in ("h1", "h2", "h3"):
            out += ["#" * int(kind[1]) + " " + block[1], ""]
        elif kind == "p":
            out += [block[1], ""]
        elif kind == "note":
            out += [f"> {block[1]}", ""]
        elif kind == "meta":
            out += [f"- **{label}**：{value}" for label, value in block[1]] + [""]
        elif kind == "ul":
            out += [f"- {item}" for item in block[1]] + [""]
        elif kind == "table":
            _, head, rows = block
            out.append("| " + " | ".join(head) + " |")
            out.append("|" + "---|" * len(head))
            out += ["| " + " | ".join(md_cell(cell) for cell in row) + " |" for row in rows]
            out.append("")
        elif kind == "figures":
            for shot in block[1]:
                out += [f"### 图{shot['figure']}　{shot.get('title', '')}", "", f"![图{shot['figure']} {shot.get('title', '')}](evidence/{shot['image']})", ""]
    return "\n".join(out).rstrip() + "\n"


def embedded_image(path: Path, max_width: int = 640) -> str:
    """Downscaled base64 JPEG, so the HTML report is a single shareable file."""
    data = b""
    if Image is not None:
        try:
            from io import BytesIO

            with Image.open(path) as image:
                image = image.convert("RGB")
                if image.width > max_width:
                    image = image.resize((max_width, round(image.height * max_width / image.width)))
                buffer = BytesIO()
                image.save(buffer, "JPEG", quality=80)
                data = buffer.getvalue()
        except OSError:
            data = b""
    if not data and shutil.which("sips"):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "image.jpg"
            result = subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80", "--resampleWidth", str(max_width), str(path), "--out", str(target)],
                capture_output=True,
            )
            if result.returncode == 0 and target.exists():
                data = target.read_bytes()
    if data:
        return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


CSS = """
body{font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;max-width:1040px;margin:40px auto;padding:0 24px;color:#1f2937}
h1{font-size:26px;margin-bottom:8px}h2{font-size:20px;margin-top:36px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}h3{font-size:16px;margin-top:24px}
dl.meta{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;background:#f9fafb;border-radius:10px;padding:14px 18px}
dl.meta dt{color:#6b7280}dl.meta dd{margin:0}
table{border-collapse:collapse;width:100%;margin:8px 0 4px;font-size:14px}th,td{border:1px solid #e5e7eb;padding:6px 10px;text-align:left;vertical-align:top}
th{background:#f3f4f6;font-weight:600}
blockquote{margin:8px 0;padding:8px 14px;background:#fffbeb;border-left:3px solid #f59e0b;color:#92400e;font-size:14px}
.figures{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}
figure{margin:0;border:1px solid #e5e7eb;border-radius:12px;padding:10px}figure img{width:100%;border-radius:8px}
figcaption{font-size:13px;color:#4b5563;margin-top:6px}
""".strip()


def to_html(blocks: list[tuple], run: Path, title: str) -> str:
    esc = html.escape
    out: list[str] = []
    for block in blocks:
        kind = block[0]
        if kind in ("h1", "h2", "h3"):
            out.append(f"<{kind}>{esc(block[1])}</{kind}>")
        elif kind == "p":
            out.append(f"<p>{esc(block[1])}</p>")
        elif kind == "note":
            out.append(f"<blockquote>{esc(block[1])}</blockquote>")
        elif kind == "meta":
            out.append("<dl class=\"meta\">" + "".join(f"<dt>{esc(label)}</dt><dd>{esc(str(value))}</dd>" for label, value in block[1]) + "</dl>")
        elif kind == "ul":
            out.append("<ul>" + "".join(f"<li>{esc(str(item))}</li>" for item in block[1]) + "</ul>")
        elif kind == "table":
            _, head, rows = block
            out.append(
                "<table><thead><tr>" + "".join(f"<th scope=\"col\">{esc(h)}</th>" for h in head) + "</tr></thead><tbody>"
                + "".join("<tr>" + "".join(f"<td>{esc(str(cell))}</td>" for cell in row) + "</tr>" for row in rows)
                + "</tbody></table>"
            )
        elif kind == "figures":
            cards = []
            for shot in block[1]:
                caption = f"图{shot['figure']}　{shot.get('title', '')}"
                src = embedded_image(run / "evidence" / shot["image"])
                cards.append(f"<figure><img src=\"{src}\" alt=\"{esc(caption)}\"><figcaption>{esc(caption)}</figcaption></figure>")
            out.append("<div class=\"figures\">" + "".join(cards) + "</div>")
    body = "\n".join(out)
    return f"<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{esc(title)}</title><style>{CSS}</style></head>\n<body>\n{body}\n</body></html>\n"


# ---------------------------------------------------------------- CLI


def chat_summary(analysis: dict[str, Any], paths: list[Path]) -> str:
    lines = [f"结果：{STATUS[analysis['status']]}", f"结论：{analysis['summary']}"]
    lines += [f"- {item}" for item in analysis["takeaways"]]
    lines += [f"报告：{path}" for path in paths]
    return "\n".join(lines)


def main() -> int:
    cli = argparse.ArgumentParser(description="把一次 app-walkthrough 走查生成报告（report.md + report.html）")
    cli.add_argument("--run", required=True, help="device.py start-run 输出的目录")
    cli.add_argument("--init", action="store_true", help="生成 analysis.json 骨架")
    cli.add_argument("--mode", choices=["ux", "collect"], help="配合 --init；默认有 collected.jsonl 时为 collect，否则为 ux")
    cli.add_argument("--force", action="store_true", help="配合 --init，覆盖已有 analysis.json")
    cli.add_argument("--draft", action="store_true", help="不需要 analysis.json，只渲染操作过程和截图")
    args = cli.parse_args()

    run = Path(args.run).expanduser().resolve()
    manifest = load_json(run / "manifest.json")
    if manifest is None:
        raise SystemExit(f"{run} 不是 start-run 创建的目录（缺少 manifest.json）")
    events = read_jsonl(run / "events.jsonl")
    shots = figures(run)
    items = {item["id"]: item for item in read_jsonl(run / "collected.jsonl")}
    analysis_path = run / "analysis.json"

    if args.init:
        if analysis_path.exists() and not args.force:
            raise SystemExit(f"{analysis_path} 已存在；要重写请加 --force")
        mode = args.mode or ("collect" if items else "ux")
        analysis_path.write_text(json.dumps(skeleton(mode, manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(analysis_path)
        comments = len(comment_index(items))
        detail = f"；帖子 {len(items)} 条（编号 p001–p{len(items):03d}，见 collected.jsonl）" if items else ""
        if comments:
            detail += f"；评论 {comments} 条（编号如 p001-c01）"
        print(f"模式：{mode}；截图 {len(shots)} 张（figures 用图号 1–{len(shots)}）{detail}")
        return 0

    steps = timeline(events, shots)
    analysis = None
    if args.draft:
        blocks = draft_blocks(manifest, steps, shots, events)
    else:
        analysis = load_json(analysis_path)
        if analysis is None:
            raise SystemExit("还没有 analysis.json。先运行 report.py --run RUN_DIR --init，填写后再生成；只看过程可以加 --draft。")
        errors = validate(analysis, shots, items)
        if errors:
            print("analysis.json 需要修改：", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 2
        if analysis["mode"] == "ux":
            blocks = ux_blocks(manifest, analysis, steps, shots, events)
        else:
            blocks = collect_blocks(manifest, analysis, items, steps, shots, events)

    title = str(blocks[0][1])
    md_path, html_path = run / "report.md", run / "report.html"
    md_path.write_text(to_markdown(blocks), encoding="utf-8")
    html_path.write_text(to_html(blocks, run, title), encoding="utf-8")
    if analysis is None:
        print(md_path)
        print(html_path)
        return 0
    if analysis["mode"] == "collect":
        themed = {ref for theme in analysis["themes"] for ref in theme["items"]}
        excluded = {ref for group in analysis.get("excluded") or [] for ref in group.get("items") or []}
        missing = [ref for ref in items if ref not in themed and ref not in excluded]
        if missing:
            print(f"提示：{len(missing)} 条帖子未归类（{'、'.join(missing)}），已列在报告附录。", file=sys.stderr)
    print(chat_summary(analysis, [md_path, html_path]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
