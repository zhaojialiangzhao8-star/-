"""独立桌面应用入口：双击即开，自带服务与页面，关窗/关闭黑窗即退。

形态：PyInstaller onedir + 系统浏览器（默认）。
之所以默认不用 pywebview 原生窗口：这台机器上 windowed 模式的 WebView2 初始化
会让进程静默退出，而浏览器路径在所有环境下都可靠。想用原生窗口：
    set FACET_WINDOW=1

数据目录：%LOCALAPPDATA%\\Facet\\data，首次运行从内置样本（bundled/）复制。

自检：
    set FACET_HEADLESS=1    只起服务不开页面（脚本/自动化用）
    %LOCALAPPDATA%\\Facet\\boot.log   启动各阶段打点，进程秒退时看这里
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path


def _boot(msg: str) -> None:
    """最早期的手写打点：不依赖 logging，windowed 模式崩了也能留下痕迹。"""
    try:
        root = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Facet"
        root.mkdir(parents=True, exist_ok=True)
        with open(root / "boot.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _base_dir() -> Path:
    """资源根：放 web/ 与 bundled/ 的地方。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent.parent  # facet/


def _module_root() -> Path:
    """`app` 包的父目录（源码模式 backend/，打包后解包目录）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent  # backend/


def _user_data_dir() -> Path:
    root = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Facet" / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _has_data(data_dir: Path) -> bool:
    import json

    cur = data_dir / "enriched" / "CURRENT.json"
    if not cur.exists():
        return False
    try:
        payload = json.loads(cur.read_text(encoding="utf-8"))
        gen = data_dir / "enriched" / "gens" / payload.get("generation", "")
        return (gen / "data.parquet").exists() and (gen / "_SUCCESS").exists()
    except Exception:
        return False


def _seed_from_bundle(data_dir: Path) -> bool:
    src = _base_dir() / "bundled"
    if not (src / "enriched" / "CURRENT.json").exists():
        return False
    dst = data_dir / "enriched"
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src / "enriched", dst)
    for extra in ("daily", "gen", "runs"):
        (data_dir / extra).mkdir(parents=True, exist_ok=True)
    return True


def _seed_strategies(data_dir: Path) -> Path:
    """把策略目录（含 enabled.json）复制到用户数据区，之后只改这一份。

    打包后 `_MEIPASS` 是临时解包目录，往里写 enabled.json 会在退出时丢失 ——
    策略库的开关必须落在持久目录，否则「今天停用的策略明天又自己回来了」。
    已存在则不覆盖，保留用户自己的启停决定。
    """
    dst = data_dir / "strategies"
    if (dst / "enabled.json").exists():
        return dst
    src = _module_root() / "app" / "strategies"
    if not src.exists():
        return dst
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.py"):
        target = dst / f.name
        if not target.exists():
            shutil.copy2(f, target)
    man = src / "enabled.json"
    if man.exists():
        shutil.copy2(man, dst / "enabled.json")
    return dst


def _free_port(preferred: int = 3021) -> int:
    for port in (preferred, 3022, 3023, 3030, 3040, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def _wait_ready(port: int, timeout: float = 60.0) -> bool:
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _acquire_lock():
    """拿独占文件锁。返回 (是否主实例, 锁文件句柄)。

    为什么不用互斥体、也不用端口探测判断：
      · PyInstaller onedir 启动时会有引导进程 + 实际进程，两个都跑 main()，
        它们几乎同时探测端口，谁都还没起来，于是**各起一个服务和窗口**
      · 命名互斥体在同一个 exe 的引导进程和实际进程之间也会互相误判

    文件锁是 OS 级独占的：拿到的那一方才是主实例，另一方的 open 直接失败，
    于是老老实实等主实例把服务起好再打开它。进程退出时句柄自动释放。
    """
    import tempfile

    root = Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir()) / "Facet"
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "instance.lock"

    try:
        f = open(lock_path, "a+b")
    except OSError:
        return True, None

    try:
        import msvcrt

        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        globals()["_LOCK_FILE"] = f      # 保持引用，进程存活期间一直持有
        return True, f
    except OSError:
        f.close()
        return False, None
    except Exception:  # noqa: BLE001  非 Windows：不锁
        return True, f


def _wait_for_instance(deadline_s: float = 40.0) -> int | None:
    """等主实例把服务起起来，返回端口。超时返回 None。"""
    import time

    end = time.time() + deadline_s
    while time.time() < end:
        port = _find_running_instance()
        if port:
            return port
        time.sleep(0.4)
    return None


def _find_running_instance() -> int | None:
    """找已经在跑的「打包版自己」，返回端口；没有就返回 None。

    判定依据只有一个：`/api/v1/health` 的 `app` 字段等于 `wenyi-desktop`。
    开发时起的 uvicorn 返回 `wenyi-dev`，不算自己 —— 否则双击 exe 会
    一声不响退出并打开开发服务的页面。

    不用命名互斥体：PyInstaller onedir 的引导进程和实际进程会各建一次，
    实测互相把对方当成"已在运行"，导致主进程直接退出。
    """
    try:
        import json
        import urllib.request

        for port in range(3021, 3041):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=0.5) as r:
                    if r.status == 200 and json.loads(r.read()).get("app") == "wenyi-desktop":
                        return port
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return None


def _open_app_window(url: str):
    """用 Edge/Chrome 的 --app 模式开「无地址栏、无标签页」的独立窗口。

    Windows 上 pywebview 要 pythonnet + WebView2 绑定，很多机器装不上；
    --app 用的是同一个 WebView2 内核，观感就是桌面软件窗口，且零依赖。
    """
    import subprocess

    candidates = [
        Path(os.getenv("PROGRAMFILES(X86)", r"C:\\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.getenv("PROGRAMFILES", r"C:\\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.getenv("PROGRAMFILES", r"C:\\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.getenv("PROGRAMFILES(X86)", r"C:\\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    ]
    for exe in candidates:
        if not exe.exists():
            continue
        try:
            # 独立 user-data-dir：否则 Edge 已开着时，新进程会把窗口「转交」给已有实例
            # 然后自己立刻退出 —— 我们等进程就会误判成「窗口关了」，服务跟着停。
            profile = Path(os.getenv("LOCALAPPDATA") or ".") / "Facet" / "webview-profile"
            profile.mkdir(parents=True, exist_ok=True)
            return subprocess.Popen(
                [
                    str(exe),
                    f"--app={url}",
                    "--window-size=1600,1000",
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                close_fds=True,
            )
        except Exception:  # noqa: BLE001
            continue
    return None


def _app_window_present(keyword: str = "文一股票策略") -> bool:
    """枚举顶层窗口，判断应用窗口是否还在（Edge --app 的进程会转交，不能靠 Popen 判活）。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if keyword in buf.value:
                found.append(buf.value)
            return True

        user32.EnumWindows(_cb, 0)
        return bool(found)
    except Exception:  # noqa: BLE001
        return True  # 探测失败就当窗口还在，宁可多活一会儿也别秒退


def main() -> int:
    _boot("enter main")

    # 独占锁：拿到锁的才是主实例。另一个进程（PyInstaller 引导层拉起的子进程、
    # 或用户手快连点）在这里直接退出，**不打开任何界面**。
    #
    # 为什么连浏览器都不开：主实例自己会开 pywebview 窗口，非主实例再开一个
    # 浏览器标签，用户看到的就是"双击打开两个软件"。关窗即退出，所以只要能
    # 走到这里，主实例的窗口一定还在，什么都不用做。
    is_primary, _lock = _acquire_lock()
    if not is_primary:
        # 主实例在跑，而且它现在有自己的原生窗口（pywebview）。
        # 再开一个窗口/标签只会让用户看到「两个软件」，所以直接退出。
        _boot("not primary, exit (primary native window owns the app)")
        return 0

    data_dir = _user_data_dir()

    # windowed 模式下 sys.stdout/stderr 是 None，任何 print 都会让进程秒退
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    import logging

    log_file = data_dir.parent / "desktop.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        handler = logging.FileHandler(str(log_file), encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
        )
        root.addHandler(handler)
    except OSError as exc:
        _boot(f"log handler failed: {exc}")
    log = logging.getLogger("facet")
    log.info("启动 data=%s frozen=%s", data_dir, getattr(sys, "frozen", False))
    _boot(f"data_dir={data_dir}")

    if not _has_data(data_dir):
        if not _seed_from_bundle(data_dir):
            log.error("没有可用数据，且包里也没有内置样本")
            _boot("no data and no bundle")
            return 2
        log.info("已从内置样本初始化数据目录")
        _boot("seeded from bundle")

    os.environ["DATA_DIR"] = str(data_dir)
    os.environ.setdefault("STATIC_DIR", str(_base_dir() / "web"))
    # 策略目录落在持久区，策略库的启停才能跨重启保留
    strat_dir = _seed_strategies(data_dir)
    os.environ.setdefault("STRATEGY_DIR", str(strat_dir))
    log.info("策略目录 %s", strat_dir)
    _boot(f"strategy_dir={strat_dir}")

    os.chdir(_base_dir())
    sys.path.insert(0, str(_module_root()))
    _boot(f"base_dir={_base_dir()} module_root={_module_root()}")

    import uvicorn

    from app.main import app  # noqa: E402
    _boot("app imported")

    port = _free_port(3021)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
    )
    threading.Thread(target=server.run, daemon=True).start()

    if not _wait_ready(port):
        log.error("服务启动失败")
        _boot(f"server not ready on {port}")
        return 3

    url = f"http://127.0.0.1:{port}/app?desktop=1"   # 默认开 React 版；旧版在 /backtest
    log.info("已就绪: %s", url)
    _boot(f"ready {url}")

    if os.getenv("FACET_HEADLESS") == "1":
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        return 0

    opened = False
    if os.getenv("FACET_WINDOW", "1") != "0":   # 默认原生窗口；FACET_WINDOW=0 退回浏览器
        try:
            import webview

            _boot("webview init")
            webview.create_window(
                "文一股票策略", url, width=1600, height=1000, min_size=(1400, 900)
            )
            webview.start()
            opened = True
        except Exception as exc:
            log.warning("webview 不可用(%s)，改用 Edge 应用窗口", exc)
            _boot(f"webview failed: {exc}")

    # pywebview 没装成（需要 pythonnet/WebView2 绑定）：用 Edge 的 --app 模式开
    # 一个没有地址栏/标签页的独立窗口，观感等同原生窗口，且不依赖任何 Python 包。
    app_proc = None
    if not opened and os.getenv("FACET_WINDOW", "1") != "0":
        app_proc = _open_app_window(url)
        _boot("edge app window opened" if app_proc else "edge app window failed")

    if app_proc is not None:
        # 不能等 Popen：Edge 会把窗口转交给 profile 里的常驻进程，然后自己退出。
        # 改成盯着窗口标题：窗口在就继续服务，窗口消失 90 秒后退出。
        last_seen = time.time()
        while True:
            time.sleep(3)
            if _app_window_present():
                last_seen = time.time()
            elif time.time() - last_seen > 90:
                break
        _boot("app window closed")
        server.should_exit = True
        return 0

    if not opened:
        import webbrowser

        webbrowser.open(url)
        _boot("browser opened")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    server.should_exit = True
    time.sleep(0.3)
    _boot("exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
