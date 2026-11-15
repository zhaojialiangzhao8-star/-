"""股票风险库 —— 规则定义。

25 条规则按用户口径拆成三档：
  · star  → *ST（退市风险警示），命中即红标 + 拉黑
  · st    → ST（风险警示），命中即拉黑
  · latent→ 隐性预埋雷（大概率次年变 ST），命中即拉黑

每条规则声明 `needs`：需要哪些字段才能判定。字段来自两处：
  · 基本面快照（data/fundamentals_snapshot.parquet）：price / mcap / name
  · 财报数据（data/risk/fundamentals.json，由 scripts/import_fundamentals.py 或人工维护）
`needs` 里出现 `fin:` 前缀的字段，缺数据时规则状态记为 `unknown`（数据缺口），
不会当成"通过"——宁可显示缺数据，也不能把有雷的票放过去。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    level: str          # star / st / latent
    title: str
    desc: str
    needs: tuple[str, ...]   # 需要的字段；"fin:xxx" = 财报字段
    auto: bool = False       # True=当前数据能自动判定


# ---- 财报字段（data/risk/fundamentals.json 里每个 symbol 一个对象）----
# deducted_net_profit  最近年报扣非净利润（元，负数=亏损）
# revenue              最近年报营业收入（元）
# net_assets           年末归属净资产（元）
# audit_opinion        unqualified | qualified | adverse | disclaimer
# fund_occupation      大股东资金占用（元）
# fund_occupation_ratio 占净资产比例（0-1）
# rectified            占用/担保是否已整改（bool）
# illegal_guarantee    违规担保金额（元）
# illegal_guarantee_ratio 占净资产比例（0-1）
# bank_account_frozen  主力银行账户被冻结（bool）
# dishonest_debtor     列入失信被执行人（bool）
# ic_nonstandard_years 内控审计连续非标年数（int）
# dividend_total_3y    近三年累计现金分红（元）
# net_profit_3y        近三年累计净利润（元）
# profit_years         连续盈利年数（int）
# rd_exempt            研发豁免（bool，双创分红规则用）
# goodwill             商誉（元）
# goodwill_ratio       商誉 / 净资产（0-1）
# pledge_ratio         实控人质押比例（0-1）
# noncore_profit_ratio 非经常性损益占净利润比例（0-1，卖资产/补贴撑利润）
# report_delayed       逾期 2 个月以上未披露定期报告（bool）
# fraud_major          财务造假触犯重大违法（bool）

STAR_RULES: tuple[Rule, ...] = (
    Rule("star_1", "star", "主板扣非亏损且营收 < 3 亿",
         "主板年度扣非净利润亏损 + 营业收入 < 3 亿",
         ("fin:deducted_net_profit", "fin:revenue")),
    Rule("star_2", "star", "双创扣非亏损且营收 < 1 亿",
         "创业板 / 科创板年度扣非净利润亏损 + 营业收入 < 1 亿",
         ("fin:deducted_net_profit", "fin:revenue")),
    Rule("star_3", "star", "年末净资产为负",
         "年末归属净资产为负数", ("fin:net_assets",)),
    Rule("star_4", "star", "年报否定意见",
         "年报出具否定审计意见", ("fin:audit_opinion",)),
    Rule("star_5", "star", "年报无法表示意见",
         "年报出具无法表示审计意见", ("fin:audit_opinion",)),
    Rule("star_6", "star", "大股东资金占用 ≥ 2 亿",
         "大股东资金占用 ≥ 2 亿", ("fin:fund_occupation",)),
    Rule("star_7", "star", "资金占用占净资产 ≥ 30% 未整改",
         "大股东资金占用占公司净资产比例 ≥ 30% 且未整改",
         ("fin:fund_occupation_ratio", "fin:rectified")),
    Rule("star_8", "star", "长期大额违规担保未解除",
         "长期大额违规担保未解除", ("fin:illegal_guarantee", "fin:rectified")),
    Rule("star_9", "star", "逾期未披露定期报告",
         "逾期两个月以上未披露年报、半年报", ("fin:report_delayed",)),
    Rule("star_10", "star", "重大财务造假",
         "财务造假数额巨大触犯监管重大违法", ("fin:fraud_major",)),
    Rule("star_11", "star", "连续 20 日股价 < 1 元",
         "连续 20 个交易日收盘价低于 1 元", ("price",), auto=True),
    Rule("star_12", "star", "主板连续 20 日市值 < 5 亿",
         "主板连续 20 个交易日总市值低于 5 亿", ("mcap",), auto=True),
    Rule("star_13", "star", "双创连续 20 日市值 < 3 亿",
         "创业板 / 科创板连续 20 个交易日总市值低于 3 亿", ("mcap",), auto=True),
)

ST_RULES: tuple[Rule, ...] = (
    Rule("st_14", "st", "违规担保 ≥ 1000 万未消除",
         "违规担保金额 ≥ 1000 万未消除", ("fin:illegal_guarantee",)),
    Rule("st_15", "st", "违规担保占净资产 ≥ 5% 未整改",
         "违规担保占净资产 ≥ 5% 未整改",
         ("fin:illegal_guarantee_ratio", "fin:rectified")),
    Rule("st_16", "st", "大股东小额资金占用长期拖欠",
         "大股东小额资金占用，长期拖欠不归还",
         ("fin:fund_occupation", "fin:rectified")),
    Rule("st_17", "st", "主力银行账户被冻结",
         "公司主力银行账户被冻结", ("fin:bank_account_frozen",)),
    Rule("st_18", "st", "列入失信被执行人",
         "公司列入失信被执行人名单", ("fin:dishonest_debtor",)),
    Rule("st_19", "st", "内控审计连续非标",
         "内控审计连续非标、内部经营混乱", ("fin:ic_nonstandard_years",)),
    Rule("st_20", "st", "主板分红不达标",
         "主板连续三年盈利、未分配利润为正，累计现金分红 < 净利润 30% 且分红总额 < 5000 万",
         ("fin:profit_years", "fin:dividend_total_3y", "fin:net_profit_3y")),
    Rule("st_21", "st", "双创分红不达标",
         "创业板 / 科创板连续三年盈利，分红总额 < 3000 万且无研发豁免",
         ("fin:profit_years", "fin:dividend_total_3y", "fin:rd_exempt")),
)

LATENT_RULES: tuple[Rule, ...] = (
    Rule("lat_22", "latent", "营收卡在临界线",
         "营收卡在 3 亿 / 1 亿临界边缘", ("fin:revenue",)),
    Rule("lat_23", "latent", "利润靠变卖资产 / 补贴",
         "净利润靠变卖资产、政府补贴撑盈利", ("fin:noncore_profit_ratio",)),
    Rule("lat_24", "latent", "商誉过高",
         "商誉数额过高（商誉 / 净资产 ≥ 30%）", ("fin:goodwill_ratio",)),
    Rule("lat_25", "latent", "实控人高比例质押",
         "实控人股权大额质押（≥ 50%）", ("fin:pledge_ratio",)),
)

# 名称识别：交易所已经戴帽的票，直接归入对应档（不算在 25 条编号规则里）
NAME_RULES: tuple[Rule, ...] = (
    Rule("name_star", "star", "*ST 退市风险警示", "交易所已实施退市风险警示（名称含 *ST）", ("name",), auto=True),
    Rule("name_st", "st", "ST 风险警示", "交易所已实施其他风险警示（名称含 ST）", ("name",), auto=True),
)

ALL_RULES: tuple[Rule, ...] = NAME_RULES + STAR_RULES + ST_RULES + LATENT_RULES
RULES_BY_ID = {r.id: r for r in ALL_RULES}

LEVEL_LABEL = {"star": "*ST 退市风险", "st": "ST 风险警示", "latent": "隐性预雷"}


def board_of(symbol: str) -> str:
    """main / gem / star_mkt / bj —— 决定营收、市值、分红的阈值。"""
    code = symbol.split(".")[0]
    if code.startswith("30"):
        return "gem"          # 创业板
    if code.startswith("68"):
        return "star_mkt"     # 科创板
    if symbol.endswith(".BJ") or code.startswith(("43", "83", "87", "92")):
        return "bj"
    return "main"
