"""基本面数据源探测：找一个当前可用的

需要的字段：
    PE / PB / PS / 股息率
    ROE / ROA / 毛利率 / 净利率 / 负债率
    营收增长 / 净利增长
    行业分类

候选源：
    1. 东财 push2（实时快照，含 PE/PB/市值）—— 之前批量被限流，测单只
    2. 东财 datacenter（财务报告）—— 需要报表接口
    3. 新浪 finance（财务摘要）
    4. 同花顺（基本面）

用法：
    python scripts/probe_fundamentals.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def get(url: str, headers: dict | None = None, timeout: int = 20) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers=headers or UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:60]}"


def probe_eastmoney_snapshot() -> None:
    """东财单只实时快照（含 PE/PB/市值）"""
    print("=== 1. 东财 push2 实时快照（单只）===")
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get?"
        "secid=1.600519&fields=f43,f57,f58,f162,f167,f116,f117,f92,f55"
    )
    ok, body = get(url)
    if ok:
        try:
            d = json.loads(body).get("data") or {}
            print(f"  ✓ 可用  {d.get('f58')}  现价 {d.get('f43')}  PE {d.get('f162')}  PB {d.get('f167')}")
            print(f"     总市值 {d.get('f116')}  流通市值 {d.get('f117')}  每股净资产 {d.get('f92')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ? 能连但解析失败: {exc}  body={body[:100]}")
    else:
        print(f"  ✗ {body}")


def probe_eastmoney_batch() -> None:
    """东财批量快照（一次多只）"""
    print("\n=== 2. 东财 push2 批量（一次 20 只）===")
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=20&po=1&np=1&fltt=2&invt=2&fid=f3&"
        "fs=m:1+t:2,m:0+t:6&fields=f12,f14,f2,f9,f23,f20,f21"
    )
    ok, body = get(url)
    if ok:
        try:
            d = json.loads(body).get("data") or {}
            items = d.get("diff") or []
            print(f"  ✓ 返回 {len(items)} 只")
            for x in items[:3]:
                print(f"     {x.get('f12')} {x.get('f14')}  价 {x.get('f2')}  PE {x.get('f9')}  PB {x.get('f23')}  市值 {x.get('f20')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ? 解析失败: {exc}")
    else:
        print(f"  ✗ {body}")


def probe_eastmoney_finance() -> None:
    """东财财务数据（ROE / 毛利率 / 增长率）"""
    print("\n=== 3. 东财 datacenter 财务指标 ===")
    # 主要财务指标
    url = (
        "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
        "reportName=RPT_F10_FINANCE_MAINFINADATA&"
        "columns=SECUCODE,REPORT_DATE,ROE_DILUTED,GROSS_PROFIT_RATIO,"
        "NETPROFIT_RATIO,TOTAL_DEBT_RATIO,EPSJB,BPS&"
        "filter=(SECUCODE=\"600519.SH\")&pageNumber=1&pageSize=5&sortColumns=REPORT_DATE&sortTypes=-1"
    )
    ok, body = get(url)
    if ok:
        try:
            d = json.loads(body)
            rows = (d.get("result") or {}).get("data") or []
            print(f"  ✓ 返回 {len(rows)} 期")
            for r in rows[:3]:
                print(
                    f"     {r.get('REPORT_DATE')}  ROE {r.get('ROE_DILUTED')}  "
                    f"毛利率 {r.get('GROSS_PROFIT_RATIO')}  净利率 {r.get('NETPROFIT_RATIO')}  "
                    f"负债率 {r.get('TOTAL_DEBT_RATIO')}"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  ? 解析失败: {exc}  body={body[:150]}")
    else:
        print(f"  ✗ {body}")


def probe_eastmoney_growth() -> None:
    """东财成长性指标"""
    print("\n=== 4. 东财 datacenter 成长性 ===")
    url = (
        "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
        "reportName=RPT_F10_FINANCE_GROWTH&"
        "columns=SECUCODE,REPORT_DATE,TOTAL_OPERATE_INCOME_YOY,PARENT_NETPROFIT_YOY&"
        "filter=(SECUCODE=\"600519.SH\")&pageNumber=1&pageSize=5&sortColumns=REPORT_DATE&sortTypes=-1"
    )
    ok, body = get(url)
    if ok:
        try:
            d = json.loads(body)
            rows = (d.get("result") or {}).get("data") or []
            print(f"  ✓ 返回 {len(rows)} 期")
            for r in rows[:3]:
                print(
                    f"     {r.get('REPORT_DATE')}  "
                    f"营收同比 {r.get('TOTAL_OPERATE_INCOME_YOY')}  "
                    f"净利同比 {r.get('PARENT_NETPROFIT_YOY')}"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  ? 解析失败: {exc}  body={body[:150]}")
    else:
        print(f"  ✗ {body}")


def probe_sina() -> None:
    """新浪财务摘要"""
    print("\n=== 5. 新浪财务摘要 ===")
    url = "https://money.finance.sina.com.cn/corp/go.php/vFD_FinanceSummary/stockid/600519.phtml"
    ok, body = get(url, headers={"User-Agent": UA["User-Agent"]})
    if ok:
        print(f"  ✓ 返回 {len(body)} 字节")
        # 简单检查是否有 ROE 等关键词
        for kw in ("净资产收益率", "每股净资产", "主营业务收入"):
            print(f"     含「{kw}」: {'是' if kw in body else '否'}")
    else:
        print(f"  ✗ {body}")


def probe_industry() -> None:
    """行业分类"""
    print("\n=== 6. 东财行业分类 ===")
    url = (
        "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
        "reportName=RPT_F10_CORETHEME_BOARDTYPE&"
        "columns=SECUCODE,BOARD_NAME,BOARD_TYPE&"
        "filter=(SECUCODE=\"600519.SH\")&pageNumber=1&pageSize=20"
    )
    ok, body = get(url)
    if ok:
        try:
            d = json.loads(body)
            rows = (d.get("result") or {}).get("data") or []
            print(f"  ✓ 返回 {len(rows)} 条")
            for r in rows[:6]:
                print(f"     {r.get('BOARD_TYPE')}: {r.get('BOARD_NAME')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ? 解析失败: {exc}")
    else:
        print(f"  ✗ {body}")


def main() -> None:
    probe_eastmoney_snapshot()
    time.sleep(1)
    probe_eastmoney_batch()
    time.sleep(1)
    probe_eastmoney_finance()
    time.sleep(1)
    probe_eastmoney_growth()
    time.sleep(1)
    probe_sina()
    time.sleep(1)
    probe_industry()
    print("\n完成")


if __name__ == "__main__":
    main()
