"""基本面数据源探测 v2：修正接口

v1 结果：
    ✓ 东财 push2 单只快照（PE/PB/市值/每股净资产）
    ✗ 东财 push2 批量（RemoteDisconnected）
    ✗ 东财 datacenter（HTTP 400，reportName 不对）
    ? 新浪（GBK 未解码）
    ✗ 东财行业分类（HTTP 400）

v2 测：
    1. emweb 财务指标接口（东财 F10）
    2. datacenter-web（换了域名）
    3. 新浪（GBK 解码）
    4. 同花顺基本面
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
    "Referer": "https://emweb.securities.eastmoney.com/",
}


def get(url: str, headers: dict | None = None, timeout: int = 20, encoding: str = "utf-8"):
    try:
        req = urllib.request.Request(url, headers=headers or UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.read().decode(encoding, errors="replace")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:60]}"


def probe_emweb_main() -> None:
    """东财 F10 主要财务指标"""
    print("=== 1. emweb 主要财务指标 ===")
    url = (
        "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
        "MainTargetAjaxNew?type=0&code=SH600519"
    )
    ok, body = get(url)
    if not ok:
        print(f"  ✗ {body}")
        return
    try:
        d = json.loads(body)
        rows = d.get("data") or []
        print(f"  ✓ 返回 {len(rows)} 期")
        if rows:
            r = rows[0]
            print(f"     报告期 {r.get('REPORT_DATE', '')[:10]}")
            for k in ("ROEJQ", "XSMLL", "XSJLL", "ZCFZL", "EPSJB", "BPS", "TOTALOPERATEREVE"):
                if k in r:
                    print(f"     {k} = {r[k]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ? 解析失败: {exc}  body={body[:200]}")


def probe_emweb_growth() -> None:
    """东财 F10 成长性"""
    print("\n=== 2. emweb 成长性指标 ===")
    url = (
        "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
        "GrowthAbilityAjaxNew?type=0&code=SH600519"
    )
    ok, body = get(url)
    if ok:
        try:
            d = json.loads(body)
            rows = d.get("data") or []
            print(f"  ✓ 返回 {len(rows)} 期")
            if rows:
                r = rows[0]
                for k, v in list(r.items())[:8]:
                    print(f"     {k} = {v}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ? 解析失败: {exc}  body={body[:150]}")
    else:
        print(f"  ✗ {body}")


def probe_datacenter_web() -> None:
    """datacenter-web（换域名）"""
    print("\n=== 3. datacenter-web 财务主表 ===")
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get?"
        "reportName=RPT_DMSK_FN_MAIN&columns=ALL&"
        "filter=(SECURITY_CODE=\"600519\")&pageNumber=1&pageSize=5&"
        "sortColumns=REPORT_DATE&sortTypes=-1"
    )
    ok, body = get(url)
    if ok:
        try:
            d = json.loads(body)
            rows = (d.get("result") or {}).get("data") or []
            print(f"  ✓ 返回 {len(rows)} 期")
            if rows:
                for k, v in list(rows[0].items())[:10]:
                    print(f"     {k} = {v}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ? 解析失败: {exc}  body={body[:200]}")
    else:
        print(f"  ✗ {body}")


def probe_sina_gbk() -> None:
    """新浪财务摘要（GBK）"""
    print("\n=== 4. 新浪财务摘要（GBK 解码）===")
    url = "https://money.finance.sina.com.cn/corp/go.php/vFD_FinanceSummary/stockid/600519.phtml"
    ok, body = get(url, headers={"User-Agent": UA["User-Agent"]}, encoding="gbk")
    if ok:
        print(f"  ✓ 返回 {len(body)} 字符")
        for kw in ("净资产收益率", "每股净资产", "主营业务收入", "每股收益"):
            print(f"     含「{kw}」: {'是' if kw in body else '否'}")
    else:
        print(f"  ✗ {body}")


def probe_ths() -> None:
    """同花顺基本面"""
    print("\n=== 5. 同花顺 F10 ===")
    url = "https://basic.10jqka.com.cn/600519/finance.html"
    ok, body = get(url, headers={**UA, "Referer": "https://basic.10jqka.com.cn/"}, encoding="gbk")
    if ok:
        print(f"  ✓ 返回 {len(body)} 字符")
        for kw in ("净资产收益率", "每股净资产", "资产负债率", "净利润"):
            print(f"     含「{kw}」: {'是' if kw in body else '否'}")
    else:
        print(f"  ✗ {body}")


def probe_push2_realtime_batch() -> None:
    """东财 push2 批量（重试，加延时）"""
    print("\n=== 6. 东财 push2 批量（重试）===")
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=20&po=1&np=1&fltt=2&invt=2&fid=f3&"
        "fs=m:1+t:2,m:0+t:6&fields=f12,f14,f2,f9,f23,f20,f21"
    )
    time.sleep(2)
    ok, body = get(url, headers={**UA, "Referer": "https://quote.eastmoney.com/center/"})
    if ok:
        try:
            d = json.loads(body).get("data") or {}
            items = d.get("diff") or []
            print(f"  ✓ 返回 {len(items)} 只")
            for x in items[:3]:
                print(
                    f"     {x.get('f12')} {x.get('f14')}  价 {x.get('f2')}  "
                    f"PE {x.get('f9')}  PB {x.get('f23')}  市值 {x.get('f20')}"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  ? 解析失败: {exc}")
    else:
        print(f"  ✗ {body}")


def main() -> None:
    probe_emweb_main()
    time.sleep(1)
    probe_emweb_growth()
    time.sleep(1)
    probe_datacenter_web()
    time.sleep(1)
    probe_sina_gbk()
    time.sleep(1)
    probe_ths()
    time.sleep(2)
    probe_push2_realtime_batch()
    print("\n完成")


if __name__ == "__main__":
    main()
