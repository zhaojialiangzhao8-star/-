"""Step 5：过滤器矩阵（在唯一可信的 P1[N60_R0.1] 上跑 G0-G7）

目的：验证「加过滤器能不能救活」。

训练集最好的一组 P1[N60_R0.1] 超额只有 +0.13pp（不显著），
验证集变负（-0.42pp），留出集也负（-0.41pp）。

这里用 G0→G7 逐个添加过滤器，看每一步的边际贡献：
    G0  无过滤
    G1  +F1 站上 MA20
    G2  +F1+F2 MA20 上升
    G3  +F1+F2+F3 量能 1.5 倍
    G4  +F1+F2+F4 不过度追高
    G5  +F1+F2+F4+F5 低价低流动性
    G6  +F1+F2+F4+F5+F6 上市满 60 日
    G7  全部

三段都跑（train/valid），看哪个过滤器在验证集上也有效。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import breakout_engine as E  # noqa: E402


def main() -> None:
    print("=" * 96)
    print("Step 5：过滤器矩阵（P1 N60 R0.1，三段对比）")
    print("=" * 96)

    # 预先把三段的基准算好，避免每组重复算
    benches = {}
    for stage in ("train", "valid"):
        benches[stage] = E.compute_benchmark(stage)
        b = benches[stage]
        print("  基准 " + stage + "：5日 " + f"{b[5] * 100:+.2f}%")

    print()
    hdr = "{:<8}{:<22}{:>8}{:>9}{:>9}{:>8}{:>7}".format(
        "组", "过滤器", "训练超额", "验证超额", "训练被拒", "验证被拒", "结论"
    )
    print(hdr)
    print("-" * 96)

    results = []
    for gname, filters in E.FILTER_GROUPS.items():
        row = {"group": gname, "filters": "+".join(filters) or "无"}
        for stage in ("train", "valid"):
            cfg = E.Config(
                platform="P1", params={"N": 60, "R": 0.10}, filters=filters, split=stage
            )
            try:
                sig = E.build_dataset(cfg)
                r = E.evaluate(sig, cfg, benches[stage])
                row[stage + "_alpha"] = (r.get("alpha_5d") or 0) * 100
                row[stage + "_reject"] = r.get("reject_pct") or 100
                row[stage + "_signals"] = r.get("signals") or 0
            except Exception as exc:  # noqa: BLE001
                row[stage + "_alpha"] = None
                row[stage + "_reject"] = None
                row[stage + "_error"] = str(exc)[:40]

        ta = row.get("train_alpha")
        va = row.get("valid_alpha")
        tj = row.get("train_reject") or 100
        vj = row.get("valid_reject") or 100
        # 判定：两边都可信 且 两边都正 → 有效
        if ta is None or va is None:
            verdict = "失败"
        elif tj > 70 or vj > 70:
            verdict = "被拒过高"
        elif ta > 0 and va > 0:
            verdict = "★有效"
        elif ta > 0 or va > 0:
            verdict = "单边正"
        else:
            verdict = "无用"

        print(
            "{:<8}{:<22}{:>9}{:>9}{:>9}{:>8}{:>7}".format(
                gname,
                row["filters"][:20],
                "{:+.2f}".format(ta) if ta is not None else "—",
                "{:+.2f}".format(va) if va is not None else "—",
                "{:.0f}%".format(tj),
                "{:.0f}%".format(vj),
                verdict,
            )
        )
        results.append(row)

    # 边际贡献
    print()
    print("每个过滤器的边际贡献（加上它之后超额变化，单位 pp）：")
    prev_t = None
    prev_v = None
    for r in results:
        ta = r.get("train_alpha")
        va = r.get("valid_alpha")
        if prev_t is not None and ta is not None:
            dt = ta - prev_t
            dv = (va - prev_v) if (va is not None and prev_v is not None) else None
            print(
                "  {:<8} 训练 {:+.2f}pp".format(
                    r["group"],
                    dt,
                )
                + (
                    "   验证 {:+.2f}pp".format(dv)
                    if dv is not None
                    else "   验证 n/a"
                )
            )
        prev_t = ta
        prev_v = va

    # 结论
    print()
    valid_ok = [r for r in results if (r.get("train_alpha") or -99) > 0 and (r.get("valid_alpha") or -99) > 0]
    if valid_ok:
        print("★ 有 {} 组在两段都正：".format(len(valid_ok)))
        for r in valid_ok:
            print(
                "   {} {}  训练 {:+.2f}  验证 {:+.2f}".format(
                    r["group"], r["filters"], r["train_alpha"], r["valid_alpha"]
                )
            )
    else:
        print("结论：没有任何过滤器组合能在训练集和验证集同时为正。")
        print("      → 加过滤器救不活，策略不成立。")

    # 落盘
    import json

    out = ROOT / "reports" / "breakout_filters.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print("→ " + str(out))


if __name__ == "__main__":
    main()
