# analysis.json format

`report.py --init` writes a skeleton; replace every `TODO`. `report.py` then validates the
file and renders `report.md` and `report.html`. The script computes layout, figure numbers,
the journey table, counts and percentages. `analysis.json` holds only judgement.

Rules for both modes:

- Keep the report about the product or the content. Appium, WDA, timing and retry details
  stay in the run log unless they cause a visible product problem.
- Tie claims to evidence. `figures` are screenshot numbers 图1…图N in capture order;
  `report.py --init` prints N. Use “未验证” for pages or controls never reached.
- Abstract volatile account data (balances, progress, coupon amounts, badges, usernames)
  in `ux` mode unless the user asked for exact values. In `collect` mode, public display
  names and post dates may be shown; nothing else from user profiles.

## Common fields

| Field | Required | Content |
|---|---|---|
| `mode` | yes | `ux` or `collect` |
| `status` | yes | `completed` / `partial` / `blocked` |
| `summary` | yes | One or two sentences that directly answer the user's question, with the key numbers |
| `takeaways` | yes, 1–5 | Most important first; each names a specific object and a count or screenshot |
| `limitations` | recommended | What was not seen or cannot be confirmed, concretely (tab, sort order, page) |
| `next_steps` | recommended | The smallest safe next run that would close a gap |

## `ux` mode

```json
{
  "mode": "ux",
  "status": "completed",
  "summary": "会员中心入口在「我的」第二屏，首页没有直达入口；权益页信息完整，但领取按钮和说明文字层级接近，容易误点。",
  "takeaways": [
    "入口偏深：从首页到会员中心需要 3 次点击（图1–图3）",
    "权益页把可享权益按类别分组，扫读效率高（图4）",
    "「立即领取」与说明文字字号相同，主次不清（图5）"
  ],
  "modules": [
    {"name": "会员中心入口", "purpose": "从「我的」进入会员体系", "seen": "「我的」页第二屏的卡片", "rating": "一般", "figures": [2]}
  ],
  "strengths": [
    {"title": "权益分组清楚", "detail": "按类别分组并显示当前可享数量", "figures": [4]}
  ],
  "issues": [
    {"severity": "Medium", "title": "领取按钮主次不清", "evidence": "按钮与说明文字字号、颜色接近", "impact": "用户可能看不出哪个是主操作", "suggestion": "按钮加粗并用主色，说明文字降级", "figures": [5]}
  ],
  "not_tested": ["未点击「立即领取」和「开通会员」", "未查看会员协议页"],
  "limitations": ["只看了已登录状态"],
  "next_steps": ["用未登录状态重走一遍入口路径"]
}
```

- `modules[].rating`: `清晰` / `一般` / `有问题`. List the modules the user asked about;
  skip tiny UI elements.
- `strengths` and `issues` each need at least one screenshot in `figures`. Write only the
  most prominent 2–5. `severity`: `High` / `Medium` / `Low`.
- Do not label an automation failure or a guessed coordinate as a product issue.
- For a long page, say in `limitations` which parts (首屏 / 中段 / 底部) were seen.
- `not_tested` lists purchase, order, claim, activation and account controls deliberately
  not clicked, and modules not visited.

## `collect` mode

Requires `RUN_DIR/collected.jsonl` from `device.py collect`. Post ids are `p001`, `p002`, …

```json
{
  "mode": "collect",
  "status": "partial",
  "question": "酷安用户讨论 PCDN 时，遇到了哪些用户体验问题",
  "keywords": ["PCDN", "MCDN"],
  "summary": "前 19 条里和用户体验直接相关的有 9 条，集中在运营商限速、设备后台偷跑和二手硬件三件事。",
  "takeaways": ["运营商处罚是最具体的痛点（3 条）：……"],
  "themes": [
    {
      "name": "运营商限速与认定",
      "description": "被运营商认定跑 PCDN 后限速、上门核查或要求签字",
      "items": ["p009", "p014", "p004"],
      "notes": {"p014": "限速一年多，要求签字才能解除"}
    }
  ],
  "excluded": [
    {"reason": "PCDN 插件推广，不反映用户体验", "items": ["p008", "p012"]}
  ],
  "limitations": ["只看了「动态」综合排序前约 16 屏"],
  "next_steps": ["在「热度」排序再采集 10 屏"]
}
```

- `question`: the user's question in their words; the report opens with it.
- `keywords`: optional. Excerpts are centred on the first hit, so long posts show the
  relevant part.
- `themes[].items`: every id must exist. A post may sit in more than one theme; the report
  says so. Counts and percentages are computed; do not write them yourself, and make sure
  numbers in `summary` and `takeaways` match.
- `themes[].notes`: optional one-line point per post, shown as a 「要点」 column. Use it for
  the posts a reader should not misread.
- `themes[].comments`: optional comment ids (`p009-c02`) that best show the theme, rendered
  as a quote table under the theme. Only when `collect --comments` was used; every id must
  exist. Prefer comments that add something the post does not: a second user confirming the
  problem, a workaround, or disagreement. When comments were collected, the theme table also
  gets a 「评论」 column, “已采集 / 页面显示总数”.
- `excluded`: posts that matched the keyword but are off-topic, with the reason.
- Posts in neither a theme nor `excluded` are listed in an appendix as 未归类 and counted
  in 采集范围. Classify everything you can.
- Read all of `collected.jsonl` before classifying. Quote user claims as claims
  (“帖子称……”). Flag reposts and one author posting several times instead of counting
  them as independent voices.
