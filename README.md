# App Walkthrough

让 AI 编程助手（Codex、OpenCode 等）通过 USB 操作你的 iPhone，一步一步走查 App，最后写成带截图的报告。

分工很简单：AI 看截图、判断下一步做什么；本仓库的脚本负责真正动手（截图、读页面文字、点击、滚动、输入），并把每一步记下来，最后生成报告。也可以用来批量采集信息流里的帖子和评论。

## 适用范围

- 只能在 macOS 上用，需要装完整的 Xcode，只有 Command Line Tools 不够。
- 需要一台真 iPhone，用数据线连着，已在 Xcode 里配对，并打开开发者模式。
- 要走查的 App 得先装在手机上。
- 不调用任何 AI 接口：截图、文字识别都在本机完成。
- 默认只做能撤回的操作。付款、下单、发帖、发消息、删除这类动作一律不做，也不会替你输入密码或验证码。

## 安装

把仓库克隆到 AI 工具的 skills 目录，文件夹名用 `app-walkthrough`，例如：

```sh
git clone git@github.com:nanqiaozhi223/app-walkthrough.git ~/.codex/skills/app-walkthrough
cd ~/.codex/skills/app-walkthrough
```

下面的命令都在这个目录里运行。

## 准备环境

先做一次检查，它只读不改：

```sh
python3 scripts/bootstrap.py
```

缺 Appium 或 xcuitest 驱动时，运行下面这条补上。它会装一对验证过能配合使用的版本（`appium@3.9.0` 和 `xcuitest@12.22.8`），不会动 Xcode：

```sh
python3 scripts/bootstrap.py --install
```

另外有几步只能你自己在 Xcode 和手机上点，每台电脑、每个 Apple 账号做一次就行：

1. Xcode → Settings → Accounts，登录 Apple 账号。免费的 Personal Team 就够用，但签名大约 7 天过期，到期要重新签。
2. 运行 `appium driver run xcuitest open-wda` 打开 WebDriverAgent 工程，给 `WebDriverAgentLib` 和 `WebDriverAgentRunner` 选上你的 Team，再把 Runner 的 Bundle ID 改成你自己的，比如 `com.yourname.WebDriverAgentRunner`。
3. Scheme 选 `WebDriverAgentRunner`，目标选你的 iPhone，点 Product → Test 装一次。
4. 在 iPhone 上打开 设置 → 通用 → VPN 与设备管理，信任这个开发者 App。

签名用的 Team ID 和 WDA 的 Bundle ID 不用手动填，脚本会自己找：Team ID 来自钥匙串里的 Apple Development 证书，Bundle ID 来自手机上装好的 WDA。只有找到多个或者一个都没找到时，才需要用 `--team-id`、`--wda-bundle-id` 指定（也可以设环境变量 `IOS_TEAM_ID`、`IOS_WDA_BUNDLE_ID`）。

最后确认一下：

```sh
python3 scripts/device.py status    # 看手机连上没有
python3 scripts/smoke_test.py       # 离线自检，不需要手机
```

## 走查一个 App

```sh
python3 scripts/device.py start-run \
  --title "settings check" \
  --task "看看设置页的入口好不好找" \
  --bundle-id com.example.app
```

它会在需要时自动启动 Appium，打开 App，然后打印一个 `RUN_DIR`。之后每条命令都带上它：

```sh
python3 scripts/device.py screenshot --run RUN_DIR --title "首页" --reason "开始"
python3 scripts/device.py texts --run RUN_DIR                       # 当前页面有哪些文字、在哪
python3 scripts/device.py tap-text --run RUN_DIR "设置" --exact --reason "进入设置"
python3 scripts/device.py screenshot --run RUN_DIR --title "设置页" --reason "确认进来了"
python3 scripts/device.py close --run RUN_DIR
```

长页面用 `scroll down --check`：滚完会和上一张截图比较，页面没动就把新截图删掉，告诉你可能已经到底了。

## 采集帖子和评论

先用正常操作进到列表页（搜关键词、选标签和排序），然后：

```sh
python3 scripts/device.py collect --run RUN_DIR \
  --label "关键词 / 动态 / 综合排序" --screens 15 --reason "采集相关帖子"
```

它会一屏屏往下滚，等内容加载完，把帖子拆开、去重，存进 `RUN_DIR/collected.jsonl`，每条有发布者、日期、正文、话题和互动数。连着两屏都没有新帖子就停。

想要评论就加 `--comments 10`：逐条点开帖子，读默认排序下的前 10 条评论（评论者、日期、内容、点赞数、楼中楼预览），再用 App 自己的返回键回到列表。一条帖子大约 40 秒。帖子和评论区里常有外链和广告，一碰就会跳到别的 App，所以它不碰广告，滚动时从屏幕右侧慢慢拖，每次操作前后都确认还在目标 App 里。一旦被带走，就立刻切回来并停下，已经采到的内容会保留。用同一个 `RUN_DIR` 再跑一次，会跳过已经处理过的帖子。

## 生成报告

```sh
python3 scripts/report.py --run RUN_DIR --init   # 生成 analysis.json，里面是待填的 TODO
# 让 AI 把 TODO 填完
python3 scripts/report.py --run RUN_DIR          # 检查无误后生成 report.md 和 report.html
```

有两种报告：体验走查（结论、功能模块、做得好的、问题和建议）和内容采集（结论、采集范围、主题分布、每个主题的帖子）。有 `collected.jsonl` 时自动用内容采集格式。只要还有没填的 TODO，或者引用了不存在的截图、帖子编号，脚本就不会生成，并告诉你哪里不对。`report.html` 把截图直接嵌在文件里，单独发给别人也能看。字段说明见 `references/report-schema.md`。

## 几个容易踩的坑

- 截图按像素算，点击按 point 算。iPhone 15 Plus 是 1290×2796 像素、430×932 point，差 3 倍。`texts` 给的已经是 point；截图或 OCR 得到的像素坐标要先除以 `screen-size` 给出的倍数。
- iPhone 没有系统返回键，返回要点 App 自己的返回或关闭按钮。
- `tap-text` 不会点“购买”“支付”“发布”“删除”“领取”这类字样，除非你明确同意，并且加上 `--allow-risky`。
- 走查记录都在 `runs/` 里。截图可能拍到账号、设备信息甚至通知内容，这个目录已被 `.gitignore` 排除，不会提交；对外分享报告前记得打码。

在 AI 工具里调用 `app-walkthrough` 这个 skill 即可，具体工作方式写在 `SKILL.md` 里。

## 致谢与许可

最初的思路来自 [Moumoon1/AI-Auto-Competitive-Analysis-SKILL](https://github.com/Moumoon1/AI-Auto-Competitive-Analysis-SKILL)（Android 版）。本仓库是 iPhone 版，代码和文档均已重新编写。

以 [MIT 许可证](LICENSE) 发布。
