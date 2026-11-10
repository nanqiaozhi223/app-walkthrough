---
name: app-walkthrough
description: Walk through an iPhone app on a USB-connected phone with Appium + XCUITest, one checked step at a time, and turn the run into a report. Use when the user wants an app's screens reviewed, a flow tried out, posts or comments gathered from a feed, or an experience/content report written from real screenshots, with no external AI API.
---

# App Walkthrough (iPhone)

You are the operator; the scripts are your hands. Each turn of the loop is the same: look at a fresh screenshot or text snapshot, decide one small action, run it with `scripts/device.py`, then look again. Everything you do is written to a run directory, and `scripts/report.py` builds the report from it.

Under the hood this is Appium 3 with the XCUITest driver, which installs WebDriverAgent (WDA) on the phone. That needs a Mac with full Xcode. Linux and Windows are not supported.

## Ground rules

Things you never do:

- Pay, order, publish, post, send, delete, subscribe, claim, change account settings, or anything else that cannot be undone. If a control might have a side effect outside the phone, stop and ask, even when it looks harmless.
- Type passwords, verification codes, ID or card numbers. Do not go looking for private messages or personal data; if they appear on screen, keep them out of the report.
- Ask for the user's Apple ID password or codes. Xcode sign-in and certificate trust are done by the user on their own devices.
- Reset the phone, uninstall or clear apps, or change system settings unless the user asked for exactly that. Sessions start with `noReset`, so app data stays as it is.

Things you always do:

- One action, then a fresh look. Never chain taps on the assumption that the first one worked.
- Stop when the screen is ambiguous, an unexpected permission prompt appears, the phone disconnects, or you are not confident the next action is safe.

## Setup

Check the machine first. This only reads:

```sh
python3 scripts/bootstrap.py
```

It reports macOS, Xcode (the full app, not only Command Line Tools), Node and npm, Appium, the xcuitest driver, and connected iPhones. If Appium or the driver is missing, tell the user what will be installed and get a yes before running `python3 scripts/bootstrap.py --install`. That installs `appium@3.9.0` and `xcuitest@12.22.8`, a pair tested together; a newer driver needs a newer Appium, so change them together or not at all. It never touches Xcode.

The rest is one-time manual work the user does, per Mac and Apple account. Guide them through it:

1. Plug in the iPhone, tap 信任 on the phone, and open Xcode → Window → Devices and Simulators so it pairs. On the phone turn on 设置 → 隐私与安全性 → 开发者模式 and let it restart.
2. Xcode → Settings → Accounts: add an Apple account. A free Personal Team is enough, but its signature lasts about 7 days and WDA must then be signed again.
3. `appium driver run xcuitest open-wda` opens the WDA project. Give the `WebDriverAgentLib` and `WebDriverAgentRunner` targets the Team under Signing & Capabilities, and change the Runner's bundle identifier to something unique, e.g. `com.<name>.WebDriverAgentRunner`. The tvOS and watchOS targets can be ignored.
4. Pick the `WebDriverAgentRunner` scheme and the phone, then Product → Test (⌘U) once. It may end with "Test Completed" or a launch error; all that matters is that it installed.
5. On the phone: 设置 → 通用 → VPN 与设备管理 → 开发者 App → the Team → 信任.

You do not need to look up the Team ID or the WDA bundle ID. `start-run` takes the Team from the one `Apple Development` certificate in the login keychain and the bundle ID from the `*.WebDriverAgentRunner.xctrunner` app on the phone, and prints both. Pass `--team-id` / `--wda-bundle-id` (or set `IOS_TEAM_ID` / `IOS_WDA_BUNDLE_ID`) only if it finds several or none.

Signing is personal. Anyone you share this skill with does steps 1–5 with their own account.

## A run, step by step

### Start

You need the app's Bundle ID, and the app must already be on the phone. If the user only gives a name, ask before reading their installed-app list, then search it:

```sh
xcrun devicectl device info apps --include-all-apps --device UDID | grep -i NAME
python3 scripts/device.py status
python3 scripts/device.py start-run --title "short name" --task "what the user asked for" --bundle-id com.example.app
```

`start-run` launches Appium on `127.0.0.1:4723` when it is not already up (log in `/tmp/appium-app-walkthrough.log`), chooses the connected iPhone (`--udid` or `IOS_UDID` if there are several), opens the app, and notes the app version and phone model. It prints `RUN_DIR`; every later command takes `--run RUN_DIR`. If it fails, no directory is created. The first start after a reboot or re-signing can take a minute or more while WDA builds.

Use one session for the whole job; it stays alive through an hour of idle time.

### Look

```sh
python3 scripts/device.py screenshot --run RUN_DIR --title "首页" --reason "开始"
python3 scripts/device.py texts --run RUN_DIR
```

Read the screenshot yourself. `texts` tells you which labels exist and where they are, in points, with icon and internal view names filtered out. `--query WORD` searches, `--min-length 20` keeps only long texts such as post bodies, and `dump-ui` saves the whole XCUITest tree when you need more.

### Act

Tapping by visible text is the safest choice:

```sh
python3 scripts/device.py tap-text --run RUN_DIR "设置" --exact --reason "进入设置"
```

It taps the innermost element with that label, and asks for `--index` when several different elements match. Labels that look like buying, paying, posting, sending, deleting, subscribing, claiming or logging out are refused; `--allow-risky` exists only for an action the user has explicitly approved.

Text that changes by itself (rotating hints, countdowns, carousels) may be gone by the time you tap. Take its coordinates from one `texts` result and use `tap` straight away.

The other actions:

```sh
python3 scripts/device.py tap --run RUN_DIR X Y --reason "..."           # points
python3 scripts/device.py tap-ratio --run RUN_DIR 0.5 0.09 --reason "..." # fraction of the screen
python3 scripts/device.py scroll --run RUN_DIR down --reason "..."        # down = show what is below
python3 scripts/device.py swipe --run RUN_DIR X1 Y1 X2 Y2 --duration-ms 450 --reason "..."
python3 scripts/device.py input --run RUN_DIR --text "测试" --reason "..."
python3 scripts/device.py key --run RUN_DIR ENTER --reason "..."
python3 scripts/device.py wait --run RUN_DIR --seconds 1.5 --reason "..."
python3 scripts/device.py launch --run RUN_DIR --reason "回到目标 App"
```

Things that differ from Android:

- There is no system back key; `key BACK` fails on purpose. Use the app's own back arrow, 关闭 or 取消, then check the screen.
- `key HOME` leaves the app. Follow it with `launch`.
- `input` types into whichever field has focus, so tap the field and confirm the keyboard is up first. `key ENTER` submits, so treat it like pressing the submit button.
- Tap, swipe, scroll and input refuse to run while another app is in front.

### Check

After every action take a new screenshot (or `texts`) and confirm you are where you meant to be. A command that exited with 0 only means the gesture was sent.

### Finish

```sh
python3 scripts/device.py close --run RUN_DIR
```

Then write the report (see below).

## Points and pixels

Screenshots are in pixels; taps are in points. On an iPhone 15 Plus that is 1290×2796 pixels against 430×932 points, a factor of 3. Run `screen-size --run RUN_DIR` once to get the factor for the phone in use.

`texts` and `dump-ui` already give points. Pixel boxes from a screenshot or from `ocr.py` must be divided by the factor. Never measure a coordinate off the image shown in the chat; it is usually scaled down. When a target is best described by its position on screen, `tap-ratio` avoids the conversion.

## When the app exposes no text

WebViews, canvases and image-only screens give `texts` little to work with. Run local OCR on the latest screenshot:

```sh
python3 scripts/ocr.py RUN_DIR/evidence/evidence-012-....png --query "会员中心"
```

It uses Apple Vision on this Mac and sends nothing anywhere. Boxes are in pixels with a top-left origin. Tap an OCR result only if the screenshot is current, the match is unambiguous, the tap lands on that text or the control it labels, and the destination is ordinary navigation. OCR never makes a purchase, activation, submit or delete button safe to press. Check the screen after the tap.

## Long pages

When a section continues below the fold, scroll with a change check:

```sh
python3 scripts/device.py scroll --run RUN_DIR down --check --title "第二屏" --reason "看下方内容"
```

The new screenshot is compared with the previous one, ignoring the status bar and the bottom strip. `changed: false` means the page did not move: the shot is deleted and you are probably at the bottom. `changed: true` keeps it. `changed: null` means the comparison failed and you judge by eye. Two unchanged results in a row means stop. `swipe-check X1 Y1 X2 Y2` does the same for a custom gesture.

Some feeds (酷安 search results, for one) draw placeholders first and fill in text a moment later. `scroll` waits 2 seconds; if `texts` still shows only tags and numbers, wait a little more before deciding the page is empty.

Four to six screens is enough to review a normal module; go further only when the user wants a feed read. Name screenshots by position (首屏 / 中段 / 底部) and say which parts you never reached. Scrolling past a card is not permission to tap it.

## Collecting posts and comments

For "find posts about X", get to the list with normal steps (search, tab, sort order), take a screenshot, and let `collect` scroll:

```sh
python3 scripts/device.py collect --run RUN_DIR --label "关键词 / 动态 / 综合排序" --screens 15 --reason "采集相关帖子"
```

On each screen it waits for the text to settle, splits the list into posts and appends new ones to `RUN_DIR/collected.jsonl` as `p001`, `p002`… with publisher, date, body, labels and interaction counts. A post seen again is merged into its earlier record. It stops after two screens with nothing new. Running it again on the same run (after changing tab or sort order) adds to the same file. Without `--comments` it never taps anything.

The roles of the fields are guessed from layout: the first long text is the body, the short text above it the publisher, numbers are counts in screen order. Check a few records against the screenshots. Lists without cells (WebViews) fall back to long texts with no publisher or date.

**Comments.** Add `--comments N` when the user wants the replies too. For each new post, `collect` taps the middle of its text, makes sure the post's own page opened (the post text or a comment box is there), reads up to N comments in the app's default order, and goes back with the app's back button. Comments are stored inside their post as `p001-c01`… with author, date, text, likes and up to three reply previews. The page's own total, such as "共 48 回复", is saved as `comment_total` and usually includes replies. Expect about 40 seconds per post; say so before a long run.

Feeds and comment sections contain links and ads that open other apps (testing ended up in 闲鱼 and WeChat). So `collect` never starts a touch on a sponsored cell, scrolls with a slow drag along the right edge instead of a swipe through the middle, and checks which app is in front before each tap and after each drag. If another app has taken over, it switches straight back, marks the post `left_app`, saves, and stops without doing anything in the other app. Take a screenshot, get back to the list, and run the same command with the same `RUN_DIR`; posts that already have a status are skipped. Tell the user about every `left_app` stop.

`comment_status` per post is one of `ok`, `not_opened` (no safe point to tap, e.g. cut off at the bottom), `wrong_page` / `no_detail` (the tap did not open the post) or `left_app`. The report counts them.

Afterwards read the whole of `collected.jsonl` and classify every post yourself, not just the first few, and never add posts that are not in the file.

## When things stall or break

Keep recovery short:

1. Give a normal page change one or two seconds, then take a screenshot.
2. Wrong tab or unchanged page: navigate from what the new screenshot shows, or use the app's back control once if that is reversible.
3. A failed command gets one retry, not a replay of the whole sequence.
4. Still stuck after one recovery step: screenshot it, mark the step blocked, move on or stop.

A fresh screenshot tells "still loading", "different tab" and "lost connection" apart faster than tapping again.

| What you see | Usually means | Do this |
|---|---|---|
| `xcodebuild failed with code 65` | WDA signing: no Team, bundle ID not unique, or the 7-day signature ran out | `start-run` names the likely cause; for expiry repeat setup step 4 |
| `Developer App Certificate is not trusted` | The phone does not trust the developer yet | Setup step 5 |
| `invalid session id` | The session timed out or was closed | `start-run` again; old element references are gone |
| No iPhone found | Locked, unplugged, or no longer paired | Unlock, replug, check Xcode → Devices |
| WDA starts and quits | Phone locked or a system alert on top | Ask the user to unlock and dismiss it |

Never "fix" a connection by uninstalling apps, resetting the phone or changing system settings.

## Writing the report

```sh
python3 scripts/report.py --run RUN_DIR --init   # writes RUN_DIR/analysis.json full of TODO
# fill in analysis.json
python3 scripts/report.py --run RUN_DIR          # checks it, then writes report.md and report.html
```

`--init` chooses `collect` mode when `collected.jsonl` exists and `ux` mode otherwise (`--mode` overrides). [references/report-schema.md](references/report-schema.md) describes every field. The script does the layout: it numbers screenshots 图1…图N, builds the step table from the run log, counts posts and percentages in collect mode, and embeds the images so `report.html` is a single shareable file. You supply the judgement. It refuses to render while anything still says `TODO`, a required field is empty, a figure number has no screenshot, or a post id is not in `collected.jsonl`; fix what it lists and run it again. `--draft` renders just the steps and screenshots to check progress.

What makes the analysis useful:

- The `summary` answers the user's question in one or two sentences with the key numbers, and still makes sense if nothing else is read. Do not open with "本次走查了…".
- Each takeaway names something concrete plus a count or a figure ("运营商限速是最具体的痛点（3 条）", not "用户对网络有抱怨"). Five at most, most important first.
- Keep what you saw apart from what you conclude. Write "帖子称…" for a user's claim instead of stating it as fact.
- Do not oversell a sample. One search with one sort order is the top N results of that search; say so, and point out reposts or one seller posting many times.
- Numbers come from the data. Theme counts are computed from `items`, and the figures in `summary` and `takeaways` must match the tables.
- `limitations` and `next_steps` name the tab, sort order, page or control that was not covered, and the smallest safe run that would cover it.

Every finding is one of three kinds, and the report must say which:

- **Confirmed**: visible in, or verified by, a fresh screenshot or text snapshot.
- **Observed**: a property of the screen you were on, not of pages you never opened.
- **Blocked / unverified**: something you meant to reach and did not.

Never describe the features or problems of a page you did not open.

By default keep account-specific values out: say "显示当前成长进度" rather than "11602/30000", and likewise for balances, dates, prices, coupon amounts, badges and user names. Give exact values only when the user asks for a snapshot or a data check, or when two pages contradict each other and the values are the evidence. For collected posts, quote text as needed but record nothing about a person beyond the display name and date shown with the post.

Automation trouble (WDA errors, retries, timing, coordinates) does not belong in the analysis unless users would see the same problem. A missed tap is not an app defect unless you reproduce it on a confirmed target with a fresh screenshot.

Before you reply, look through the rendered report: every issue points at a screenshot that shows it, and every module says what was and was not reached. Move screenshots that show notifications or other private content out of `RUN_DIR/evidence/` before rendering. Then answer from the summary the script prints: conclusion first, the main takeaways, the main limitation, and a link to the report, shorter than the report itself and without its tables.

## Habits that keep runs safe

- Only start runs for an app the user named; `launch` returns to that same app.
- Text before coordinates. Use `tap` on point coordinates from the current `texts` output only when the screen and the target are unambiguous and the action is low-risk.
- `scroll` for page content, `swipe` for special gestures such as carousels. A gesture's coordinates are invalid as soon as the layout changes.
- Asking the user means stopping and asking in the chat. Never invent an approval for a risky action.
- About 12 actions is a sensible budget for an exploratory run unless the user wants more.
- Two snapshots in a row that look the same after an action: do not repeat it. Try one recovery step or report the block.

## Files

- `scripts/device.py`: phone control and run logging (start/close, screenshots, texts, taps, scrolling with change checks, feed and comment collection, input).
- `scripts/bootstrap.py`: environment check; `--install` adds the pinned Appium and driver once the user agrees.
- `scripts/report.py`: `--init` writes the `analysis.json` skeleton; without flags it validates and writes `report.md` and `report.html`.
- `scripts/ocr.py` + `scripts/vision_ocr.swift`: offline Apple Vision OCR, pixel boxes.
- `scripts/smoke_test.py`: offline tests for the report, the parsers and the helpers; no phone needed.
- `references/report-schema.md`: the `analysis.json` format for both report modes.
