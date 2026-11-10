#!/usr/bin/env python3
"""Check (and with --install, prepare) what app-walkthrough needs on this Mac."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from device import list_iphones
except Exception as _import_err:  # pragma: no cover
    def list_iphones() -> list[dict[str, Any]]:
        raise RuntimeError(f"无法加载 device 模块: {_import_err}")

# These two versions are validated as a pair; a newer xcuitest line needs a newer Appium.
DEFAULT_APPIUM_VERSION = "3.9.0"
DEFAULT_XCUITEST_VERSION = "12.22.8"

CHECK_TIMEOUT_SECONDS = 60
INSTALL_TIMEOUT_SECONDS = 900


def run_cmd(cmd: list[str], timeout: int = CHECK_TIMEOUT_SECONDS) -> tuple[int, str]:
    """Run subprocess command, merging stderr into stdout, never raising."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except FileNotFoundError:
        return 127, f"未找到可执行文件: {cmd[0]}"
    except subprocess.TimeoutExpired as exc:
        output = ""
        if exc.stdout:
            output = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout)
        return 124, f"命令执行超时 ({timeout}s): {' '.join(cmd)}\n{output}".strip()
    except Exception as exc:
        return 1, f"命令执行异常: {exc}"


def is_executable(path: str | Path | None) -> bool:
    if not path:
        return False
    p = Path(path).expanduser()
    return p.is_file() and os.access(str(p), os.X_OK)


def parse_semver(version_text: str) -> tuple[int, int, int] | None:
    """Extract (major, minor, patch) from a version string."""
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", version_text.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def extract_json_object(raw_text: str) -> dict[str, Any] | None:
    """Extract a JSON object from output that may contain preceding/trailing log lines."""
    text = raw_text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def locate_appium() -> str | None:
    """Locate the appium binary across env, PATH, ~/.npm-global, and npm prefix."""
    env_bin = os.environ.get("APPIUM_BIN")
    if env_bin and is_executable(env_bin):
        return str(Path(env_bin).expanduser().resolve())

    which_bin = shutil.which("appium")
    if which_bin and is_executable(which_bin):
        return which_bin

    npm_global_candidate = Path("~/.npm-global/bin/appium").expanduser()
    if is_executable(npm_global_candidate):
        return str(npm_global_candidate)

    npm_bin = shutil.which("npm")
    if npm_bin:
        code, prefix_out = run_cmd([npm_bin, "config", "get", "prefix"])
        if code == 0 and prefix_out:
            lines = [line.strip() for line in prefix_out.splitlines() if line.strip()]
            if lines:
                prefix_candidate = Path(lines[-1]).expanduser() / "bin" / "appium"
                if is_executable(prefix_candidate):
                    return str(prefix_candidate)

    return None


def check_macos() -> dict[str, Any]:
    return {
        "name": "macos",
        "ok": sys.platform == "darwin",
        "message": platform.platform(),
    }


def check_xcode() -> dict[str, Any]:
    xcodebuild_bin = shutil.which("xcodebuild")
    code_sel, dev_dir_out = run_cmd(["xcode-select", "-p"])
    dev_dir = dev_dir_out.splitlines()[0].strip() if (code_sel == 0 and dev_dir_out) else ""

    if code_sel != 0 or not dev_dir:
        return {
            "name": "xcode",
            "ok": False,
            "message": "未配置 Xcode 开发者目录（xcode-select -p 失败），请安装完整 Xcode",
        }

    if "CommandLineTools" in dev_dir or dev_dir.startswith("/Library/Developer/CommandLineTools"):
        return {
            "name": "xcode",
            "ok": False,
            "path": dev_dir,
            "message": f"当前开发者目录为 Command Line Tools ({dev_dir})，需要完整 Xcode",
        }

    if not xcodebuild_bin:
        return {
            "name": "xcode",
            "ok": False,
            "path": dev_dir,
            "message": "未找到 xcodebuild，请安装完整 Xcode",
        }

    code_ver, ver_out = run_cmd([xcodebuild_bin, "-version"])
    if code_ver != 0 or not ver_out:
        return {
            "name": "xcode",
            "ok": False,
            "path": dev_dir,
            "message": f"xcodebuild -version 执行失败: {ver_out or '无输出'}",
        }

    first_line = ver_out.splitlines()[0].strip()
    if not first_line.lower().startswith("xcode"):
        return {
            "name": "xcode",
            "ok": False,
            "path": dev_dir,
            "message": f"未检测到完整 Xcode 版本信息: {first_line}",
        }

    return {
        "name": "xcode",
        "ok": True,
        "path": dev_dir,
        "message": first_line,
    }


def check_node() -> dict[str, Any]:
    node_bin = shutil.which("node")
    if not node_bin:
        return {
            "name": "node",
            "ok": False,
            "message": "未找到 node，请先安装 Node.js",
        }
    code, out = run_cmd([node_bin, "--version"])
    if code != 0 or not out:
        return {
            "name": "node",
            "ok": False,
            "path": node_bin,
            "message": f"获取 node 版本失败: {out or '无输出'}",
        }
    return {
        "name": "node",
        "ok": True,
        "path": node_bin,
        "message": out.splitlines()[-1].strip(),
    }


def check_npm() -> dict[str, Any]:
    npm_bin = shutil.which("npm")
    if not npm_bin:
        return {
            "name": "npm",
            "ok": False,
            "message": "未找到 npm，请先安装 Node.js",
        }
    code, out = run_cmd([npm_bin, "--version"])
    if code != 0 or not out:
        return {
            "name": "npm",
            "ok": False,
            "path": npm_bin,
            "message": f"获取 npm 版本失败: {out or '无输出'}",
        }
    return {
        "name": "npm",
        "ok": True,
        "path": npm_bin,
        "message": out.splitlines()[-1].strip(),
    }


def check_appium() -> dict[str, Any]:
    appium_bin = locate_appium()
    if not appium_bin:
        return {
            "name": "appium",
            "ok": False,
            "supported": False,
            "message": "未找到 Appium（需要 3.0.0 或更高）",
        }

    code, out = run_cmd([appium_bin, "--version"])
    if code != 0 or not out:
        return {
            "name": "appium",
            "ok": False,
            "path": appium_bin,
            "supported": False,
            "message": f"无法获取 Appium 版本: {out or '无输出'} (需要 3.0.0 或更高)",
        }

    ver_line = out.splitlines()[-1].strip()
    semver = parse_semver(ver_line)
    supported = semver is not None and semver >= (3, 0, 0)
    message = ver_line if supported else f"{ver_line} (需要 3.0.0 或更高)"

    return {
        "name": "appium",
        "ok": supported,
        "path": appium_bin,
        "supported": supported,
        "message": message,
    }


def check_xcuitest_driver(appium_bin: str | None = None) -> dict[str, Any]:
    resolved_appium = appium_bin or locate_appium()
    if not resolved_appium:
        return {
            "name": "xcuitest-driver",
            "ok": False,
            "message": "需要先安装 Appium 才能检查 xcuitest 驱动",
        }

    code, out = run_cmd([resolved_appium, "driver", "list", "--installed", "--json"])
    if code != 0:
        return {
            "name": "xcuitest-driver",
            "ok": False,
            "path": resolved_appium,
            "message": f"查询已安装 Appium 驱动失败: {out or '无输出'}",
        }

    drivers = extract_json_object(out)
    if drivers is None:
        return {
            "name": "xcuitest-driver",
            "ok": False,
            "path": resolved_appium,
            "message": "无法解析 Appium 驱动列表 JSON 输出",
        }

    xcuitest_info = drivers.get("xcuitest")
    if not isinstance(xcuitest_info, dict):
        return {
            "name": "xcuitest-driver",
            "ok": False,
            "path": resolved_appium,
            "message": "未安装 xcuitest 驱动",
        }

    version = xcuitest_info.get("version") or "unknown"
    return {
        "name": "xcuitest-driver",
        "ok": True,
        "path": resolved_appium,
        "message": f"xcuitest@{version}",
    }


def check_ios_device() -> dict[str, Any]:
    try:
        devices = list_iphones()
        if not isinstance(devices, list):
            devices = []
    except Exception as exc:
        return {
            "name": "ios-device",
            "ok": False,
            "devices": [],
            "message": f"检测 iPhone 设备失败（{exc}）：请通过 USB 连接 iPhone，解锁屏幕，点击“信任此电脑”，并在设置中启用“开发者模式”",
        }

    connected_devices = [d for d in devices if isinstance(d, dict) and d.get("connected") is True]
    if not connected_devices:
        return {
            "name": "ios-device",
            "ok": False,
            "devices": devices,
            "message": "未检测到已连接的 iPhone：请通过 USB 连接设备、解锁屏幕、点击“信任此电脑”，并在设置中开启“开发者模式”",
        }

    parts: list[str] = []
    for dev in connected_devices:
        model = dev.get("model") or dev.get("name") or "iPhone"
        os_version = dev.get("os_version") or "未知系统版本"
        desc = f"{model} iOS {os_version}"

        warnings: list[str] = []
        dev_mode = dev.get("developer_mode")
        if dev_mode != "enabled":
            warnings.append(f"开发者模式未启用(developer_mode={dev_mode})")
        pairing = dev.get("pairing")
        if pairing != "paired":
            warnings.append(f"设备尚未完成配对信任(pairing={pairing})")

        if warnings:
            desc = f"{desc} [{'，'.join(warnings)}]"
        parts.append(desc)

    return {
        "name": "ios-device",
        "ok": True,
        "devices": devices,
        "message": f"iPhone connected: {'; '.join(parts)}",
    }


def run_all_checks() -> list[dict[str, Any]]:
    macos_res = check_macos()
    xcode_res = check_xcode()
    node_res = check_node()
    npm_res = check_npm()
    appium_res = check_appium()
    xcuitest_res = check_xcuitest_driver(appium_res.get("path"))
    ios_res = check_ios_device()
    return [
        macos_res,
        xcode_res,
        node_res,
        npm_res,
        appium_res,
        xcuitest_res,
        ios_res,
    ]


def compute_exit_code(results: list[dict[str, Any]]) -> int:
    for item in results:
        if item.get("name") == "ios-device":
            continue
        if not item.get("ok"):
            return 1
    return 0


def print_human_results(results: list[dict[str, Any]]) -> None:
    for item in results:
        status = "[OK]" if item.get("ok") else "[缺失]"
        print(f"{status} {item['name']}: {item['message']}")

    by_name = {item["name"]: item for item in results}
    non_device_failed = [item for item in results if item["name"] != "ios-device" and not item.get("ok")]

    print()
    if not non_device_failed:
        print(
            "提示：首次使用时，请在 Xcode 中为 WebDriverAgentRunner 配置签名 Team，"
            "并在 iPhone 的“设置 -> 通用 -> VPN 与设备管理”中信任开发者证书。"
        )
        return

    print("下一步修复建议：")
    if not by_name.get("macos", {}).get("ok"):
        print("  - 请在 macOS 系统上运行此工具。")
    if not by_name.get("xcode", {}).get("ok"):
        print(
            "  - 请从 App Store 安装完整 Xcode，安装后自行在终端执行："
            "sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
        )
    if not by_name.get("node", {}).get("ok") or not by_name.get("npm", {}).get("ok"):
        print("  - 请先安装 Node.js（包含 node 与 npm），例如从 https://nodejs.org 下载安装。")
    if not by_name.get("appium", {}).get("ok") or not by_name.get("xcuitest-driver", {}).get("ok"):
        print("  - 运行自动安装命令：python3 scripts/bootstrap.py --install")


def tail_text(text: str, max_lines: int = 20) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


def perform_install(appium_version: str, xcuitest_version: str) -> int:
    npm_check = check_npm()
    if not npm_check.get("ok"):
        print("错误：未找到可用的 npm，请先安装 Node.js 后再运行 --install。", file=sys.stderr)
        return 1

    npm_bin = npm_check.get("path") or "npm"
    appium_bin = locate_appium()

    if appium_bin is None:
        print(f"正在安装 Appium (appium@{appium_version})...")
        code, out = run_cmd([npm_bin, "install", "--global", f"appium@{appium_version}"], timeout=INSTALL_TIMEOUT_SECONDS)
        if code != 0:
            print(f"安装 appium@{appium_version} 失败 (退出码 {code})：", file=sys.stderr)
            if out:
                print(tail_text(out), file=sys.stderr)
            return 1
        appium_bin = locate_appium()
        if appium_bin is None:
            print(
                "错误：已完成 Appium 安装，但仍无法定位 appium 可执行文件。"
                "请将 npm 全局 bin 目录添加到 PATH，或设置环境变量 APPIUM_BIN。",
                file=sys.stderr,
            )
            return 1
    else:
        print(f"Appium 已安装 ({appium_bin})，跳过安装。")

    driver_check = check_xcuitest_driver(appium_bin)
    if driver_check.get("ok"):
        print(f"xcuitest 驱动已安装 ({driver_check.get('message')})，跳过安装。")
    else:
        print(f"正在安装 xcuitest 驱动 (xcuitest@{xcuitest_version})...")
        code, out = run_cmd(
            [appium_bin, "driver", "install", f"xcuitest@{xcuitest_version}"],
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
        if code != 0:
            print(f"安装 xcuitest@{xcuitest_version} 失败 (退出码 {code})：", file=sys.stderr)
            if out:
                print(tail_text(out), file=sys.stderr)
            return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检查或准备 app-walkthrough 的 iPhone 运行环境",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="以 JSON 列表格式输出检查结果",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="安装缺失的 Appium 与 xcuitest 驱动，然后执行检查",
    )
    parser.add_argument(
        "--appium-version",
        default=DEFAULT_APPIUM_VERSION,
        help=f"指定安装的 Appium 版本（默认: {DEFAULT_APPIUM_VERSION}）",
    )
    parser.add_argument(
        "--xcuitest-version",
        default=DEFAULT_XCUITEST_VERSION,
        help=f"指定安装的 xcuitest 驱动版本（默认: {DEFAULT_XCUITEST_VERSION}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.install:
        install_rc = perform_install(args.appium_version, args.xcuitest_version)
        if install_rc != 0:
            return install_rc

    results = run_all_checks()
    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print_human_results(results)

    return compute_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
