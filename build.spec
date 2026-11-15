# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：把回测台打成单文件 GUI 程序。

    cd facet
    python -m PyInstaller --noconfirm --clean build.spec

产物：dist/Facet回测台.exe
"""
from PyInstaller.utils.hooks import collect_all

# polars 的 runtime 是独立二进制，必须整包收集，否则运行时报 schema 缺失
datas, binaries, hiddenimports = collect_all("polars")

# 原生窗口：pywebview + pythonnet（WebView2 后端）。三个包整包收集，
# 否则 frozen 后报 "No module named 'webview'"，又退回浏览器标签。
for _pkg in ("webview", "pythonnet", "clr_loader"):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass

datas += [
    # 策略插件目录必须带上（frozen 下按目录扫描文件）
    ("backend/app/strategies", "app/strategies"),
    ("backend/app/mcp", "app/mcp"),
    ("frontend/dist", "frontend/dist"),
]

hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "app",
    "app.main",
    "app.api",
    "app.runtime",
    "app.engine",
    "app.store",
    "app.indicators",
    "app.parquet",
    "app.backtest_stats",
    "app.backtest",
    "app.backtest.engine",
    "app.backtest.data",
    "app.backtest.models",
    "app.backtest.broker",
    "app.backtest.portfolio",
    "app.backtest.risk",
    "app.backtest.metrics",
    "app.config",
    "app.strategies",
    "app.mcp.server",
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr",
    "pythonnet",
    "app.infrastructure.storage.db",
    "app.infrastructure.realtime.quotes",
    "app.modules.monitor.realtime_api",
]

a = Analysis(
    ["backend/app/desktop.py"],
    pathex=["backend"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 只留下真正要用的：polars 自带 parquet 读写，不需要 pyarrow/pandas
    excludes=["pyarrow", "pandas", "matplotlib", "IPython", "tkinter", "pytest", "test"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# 走 onedir 而不是 onefile：
# 这台机器上 PyInstaller 单文件的 bootloader 报 "Could not create temporary directory"
# （%TEMP% 里有大量历史 _MEI 残留 + 安全软件行为），onedir 不需要解包，启动也快得多。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="文一股票策略",
    icon="assets/app.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI：不弹黑窗，日志写 %LOCALAPPDATA%\Facet\desktop.log
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="文一股票策略",
)
