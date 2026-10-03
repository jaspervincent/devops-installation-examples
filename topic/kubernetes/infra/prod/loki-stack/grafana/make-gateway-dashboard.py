#!/usr/bin/env python3
"""nginx-gateway-fabric 访问日志看板（JSON 格式）

前提：NginxProxy 已配置 logging.accessLog.escape=json + 自定义 JSON format。

两个坑（都实测踩过）：
 1. 同一个容器里混着 nginx-agent 的 logfmt 日志和 JSON access log，
    直接 `| json` 会在 agent 日志上产生 JSONParserErr。必须先用 `|~ ^\\{` 过滤。
 2. Pod 的 init 容器也输出 JSON（"Starting init container"），
    同样能通过 ^\\{ 但没有 status 字段。用 container="nginx" + status!="" 双保险排除。

Grafana 的 Loki 数据源不插值 $__rate_interval，窗口统一用 $rate_window。
"""
import json, subprocess

DS_UID = "efzm3ndgkowsga"
GRAFANA = "http://10.0.0.101:3000"
AUTH = "admin:admin"

ds = {"type": "loki", "uid": DS_UID}
W = "$rate_window"

# 基础选择器：精确定位到 JSON access log
#
# 这里【刻意没有】namespace 过滤。实测每个网关 app 只存在于一个命名空间
# （gateway-public-nginx->default、loki-read-gateway->loki、web-gateway->demo-app），
# app 已经唯一确定了命名空间，namespace 变量纯属冗余——而且是个陷阱：
# 它的候选级联自 {app=~"$gateway"}，用户在 gateway=All 时选中 loki，
# 再把 gateway 切成 gateway-public-nginx（在 default），两者矛盾，
# 13 个面板会全部变空，且 Grafana 把变量记在 URL 里，刷新也不会自己恢复。
# 将来真出现同名网关跨命名空间时，再把它加回来。
#
# host 放在【流选择器】里而不是 `| host =~` 行过滤：Alloy 已经把它提升为索引标签
# （带白名单归一化，见 config.alloy），在流选择器里过滤走索引，比解析完再过滤快。
# 注意 `| json` 之后 JSON 里的 host 字段会和这个同名 stream label 冲突，
# Loki 会把解析出来的那个重命名为 host_extracted——按 host 分组时拿到的是
# 归一化后的标签值（基数受控），这正是我们要的。
# ---- 项目 / 环境 ----
# 这两个是最高层级的筛选，排在级联链首。目前各只有一个取值
# （project=jp / environment=test），但链路先铺好，
# 将来接入第二个项目或环境时，看板不用再动。
PE = 'project=~"$project", environment=~"$environment"'

# label_format 把查询串从 uri 里剥掉，得到归一化的 path。
#
# **这是生产环境的硬需求**：uri 带着查询串，接口表里每个 ?id=123 都会单独
# 成一行，表格直接炸掉。实测本环境 /login 原样是 308 行次，归一化后 378
# （把 ?echo_code= 的变体合并进来了）。
# 用的是 LogQL label_format 里的 sprig regexReplaceAll，实测可用。
# 原始 uri 仍保留，「流量来源」折叠行里有带查询串的完整 URI 排行。
BASE = ('{%s, app=~"$gateway", container="nginx", host=~"$host"} '
        '|~ `^\\{` | json | status != "" '
        '| label_format path=`{{ regexReplaceAll "\\\\?.*$" .uri "" }}`' % PE)
# 再叠加顶部的 host / 状态码 / 关键字过滤。
#
# status 用正则而不是数值比较：正则能一条表达式覆盖「某一档」（4..）和
# 「若干档」（[45]..），做成下拉更直观；数值比较要写成
# `| status >= 400 | status < 500` 两段，没法塞进一个变量。
# 实测两种写法都可用——| json 会把 status 变成字符串 label，=~ 正常工作，
# 同时它仍是数字，别处的 `| status >= 400` 也照样能用。
FLT = '| status =~ "$status" |= "$search"'
SEL = BASE + " " + FLT


def lv(name, label, stream=None, allv=True, label_name=None):
    """label_values 型模板变量。

    label_name 用来把「变量名」和「要查的标签名」分开。默认两者相同，
    但本看板的 gateway 变量要查的是 app 标签——Loki 里没有 gateway 这个标签，
    不分开的话下拉框会是空的（曾经就是空的，而且空下拉框 + All 插值
    一起把 namespace 级联查询打成了 {app=~".*"}，直接报 parse error）。
    """
    q = {"label": label_name or name, "refId": name, "type": 1}
    if stream:
        q["stream"] = stream
    # allValue 必须显式给 ".+"。留空时 Grafana 把 All 插值成 ".*"，
    # 而 namespace 变量的级联查询只有 {app=~"$gateway"} 这一个 matcher，
    # 变成 {app=~".*"} 后 Loki 直接拒绝：
    #   queries require at least one regexp or equality matcher
    #   that does not have an empty-compatible value
    # ".+" 要求标签存在且非空，语义上正好是「全部」。
    return {"name": name, "label": label, "type": "query", "datasource": ds,
            "query": q, "refresh": 2, "includeAll": allv, "multi": True,
            "allValue": ".+",
            "sort": 1, "current": {"text": ["All"], "value": ["$__all"]}}


# ---- 日志级别过滤 ----
# 用 detected_level（Loki 服务端推断，结构化元数据）而不是索引标签 level。
# 实测（近 1h，全栈）：索引标签 level 只覆盖 4276/89069 ≈ 4.8% 的日志——
# 只有 journal、k8s 事件、网关访问日志三类在采集时打了它，绝大多数 Pod 日志没有。
# 拿它当筛选条件会让 95% 的日志凭空消失，正是 README 坑位 25 那一类问题。
#
# detected_level 覆盖 100%（含 unknown 一档），而且顺带修掉了一个不一致：
# 索引标签里 journal 打的是 "warning"、k8s 事件打的是 "warn"，两种拼法并存；
# detected_level 统一归一成 "warn"。
#
# 它是结构化元数据不是标签，因此：
#   1) 不能写进 {} 里，只能作为管道阶段  | detected_level=~"..."
#   2) label_values(detected_level) 返回空，变量必须用 custom 类型硬编码取值
LEVELS = ["trace", "debug", "info", "notice", "warn",
          "error", "critical", "fatal", "unknown"]
LVL = '| detected_level=~"$level"'


def with_level(expr):
    """在流选择器的 } 之后插入级别过滤。

    本栈每条表达式只有一个流选择器，所以取第一个 } 即可。
    找不到就直接报错，不要静默跳过——静默跳过会变成「某几个面板不受级别过滤
    影响」，而这种不一致从界面上完全看不出来。
    """
    i = expr.find("}")
    if i < 0:
        raise ValueError("表达式里找不到流选择器: " + expr)
    return expr[:i + 1] + " " + LVL + " " + expr[i + 1:]


def level_var():
    return {
        "name": "level", "label": "级别", "type": "custom",
        "query": ",".join(LEVELS),
        "options": [{"selected": False, "text": v, "value": v} for v in LEVELS],
        "current": {"text": ["All"], "value": ["$__all"]},
        "includeAll": True, "multi": True, "allValue": ".+",
        "refresh": 0, "skipUrlSync": False,
        "description": "按 Loki 推断的 detected_level 过滤，覆盖全部日志。"
                       "不用索引标签 level：它只覆盖约 5% 的日志，"
                       "用它筛会让绝大多数 Pod 日志消失。",
    }


def tgt(expr, legend=None, ref="A", level=True, qtype="range"):
    t = {"refId": ref, "datasource": ds,
         "expr": with_level(expr) if level else expr, "queryType": qtype}
    if legend:
        t["legendFormat"] = legend
    return t


def panel(pid, title, ptype, gp, targets, desc="", extra=None):
    p = {"id": pid, "title": title, "type": ptype, "datasource": ds,
         "gridPos": gp, "targets": targets, "description": desc}
    if extra:
        p.update(extra)
    return p


TS = {"fieldConfig": {"defaults": {
        "custom": {"lineWidth": 1, "fillOpacity": 15, "showPoints": "never"},
        "unit": "short"}, "overrides": []},
      "options": {"legend": {"displayMode": "list", "placement": "bottom",
                             "showLegend": True},
                  "tooltip": {"mode": "multi", "sort": "desc"}}}


def stat(dec, unit="short", steps=None, novalue="0"):
    """noValue：无结果时显示 0 而不是 "No data"——「这段时间没有请求」
    和「采集断了」语义不同，No data 把两者混成一样。"""
    fc = {"unit": unit, "decimals": dec, "noValue": novalue}
    if steps:
        fc["color"] = {"mode": "thresholds"}
        fc["thresholds"] = {"mode": "absolute", "steps": steps}
    return {"fieldConfig": {"defaults": fc, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                        "colorMode": "value" if steps else "none",
                        "graphMode": "area"}}


def row(pid, title, y, collapsed=False, children=None):
    """折叠行。子面板必须嵌在 row["panels"] 里；
    **顶层面板必须排在所有 row 之前**，否则会被算进某个 row 的区段跟着折叠掉。
    """
    return {"id": pid, "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "panels": list(children or [])}


RANGE_W = "$__range"     # 仪表盘时间范围；本 Grafana 的 Loki 数据源会插值它
LIST_N = 30              # topk 封顶


# 柱状堆叠时序。配面板级 interval 设最小步长——不设的话 step 可能只有一两秒，
# 画出来是一排密集栅栏（研发看板踩过）。
BARS = {"fieldConfig": {"defaults": {
            "custom": {"drawStyle": "bars", "fillOpacity": 80, "lineWidth": 0,
                       "stacking": {"mode": "normal", "group": "A"}},
            "unit": "short"}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom",
                               "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}}}


def _table_base(idx, renames, overrides=None):
    """表格面板的公共配置。

    Loki 的 instant 查询返回「每序列一个 frame」，字段只有 Time/Value，
    标签挂在 field.labels 上——所以必须 labelsToFields 摊成列、merge 并表。
    四个变换的顺序不能动：labelsToFields -> merge -> organize -> sortBy。

    merge 之后值字段带 refId 后缀（"Value #A"），改名要按这个名字，
    改错了不报错，列名和排序一起静默失效。
    """
    return {
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "fields": ""}},
        "fieldConfig": {"defaults": {"unit": "short",
                                     "custom": {"align": "auto"}},
                        "overrides": overrides or []},
        "transformations": [
            {"id": "labelsToFields", "options": {"mode": "columns"}},
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": {"Time": True},
                                           "indexByName": idx,
                                           "renameByName": renames}},
        ],
    }


def table1(value_label, renames, order):
    """单查询表格：标签列 + 一个值列。"""
    rn = dict(renames)
    rn["Value #A"] = value_label
    rn["Value"] = value_label
    idx = {"Time": 0}
    for n, f in enumerate(list(order) + ["Value #A", "Value"], start=1):
        idx[f] = n
    cfg = _table_base(idx, rn)
    cfg["transformations"].append(
        {"id": "sortBy", "options": {"fields": {},
                                     "sort": [{"field": value_label, "desc": True}]}})
    return cfg


def table_endpoints():
    """接口使用情况表：三条查询（请求数 / 错误率 / P95）按 host+method+path 合并。

    实验台验证过：merge 能按共同的标签列对齐三组 frame，六列齐全。
    错误率列设 noValue="0"——没有 4xx/5xx 的接口在 B 查询里**没有序列**，
    不设的话那格是空白，看着像缺数据。
    """
    idx = {"Time": 0, "host": 1, "method": 2, "path": 3,
           "Value #A": 4, "Value #B": 5, "Value #C": 6}
    rn = {"host": "域名", "method": "方法", "path": "接口",
          "Value #A": "请求数", "Value #B": "错误率", "Value #C": "P95"}
    ov = [
        {"matcher": {"id": "byName", "options": "错误率"},
         "properties": [{"id": "unit", "value": "percent"},
                        {"id": "decimals", "value": 1},
                        {"id": "noValue", "value": "0"}]},
        {"matcher": {"id": "byName", "options": "P95"},
         "properties": [{"id": "unit", "value": "s"},
                        {"id": "decimals", "value": 2}]},
    ]
    cfg = _table_base(idx, rn, ov)
    cfg["transformations"].append(
        {"id": "sortBy", "options": {"fields": {},
                                     "sort": [{"field": "请求数", "desc": True}]}})
    return cfg


DOC = """\
### 这个看板回答什么

**所有业务域名的统一入口上，发生了什么、谁在用、用得怎么样。**

两个用途共用一张表：顶部的「接口使用情况」同时回答
*哪个接口最常用*（业务）和 *哪个接口有问题*（排障）。

### 第一屏怎么读

前五个指标是 RED 三件套加业务量：
*QPS（Rate）→ 错误率（Errors）→ P95（Duration）→ 传输流量 → 独立客户端*。

往下是请求趋势和按域名的分布，再往下是接口表。
排障细节（4xx/5xx 原文、慢请求、网关 vs 后端耗时）在「错误与慢请求」折叠行里。

### 几个容易误解的地方

- **错误率阈值按生产标准定（1% 橙 / 5% 红），不是按本环境基线。**
  本测试环境的 demo 应用**故意**返回约 13.6% 的错误（404/500/503），
  所以这里会一直显示红色——那是预期，不是故障。
- **接口列已剥掉查询串**（`/api/orders?id=1` 和 `?id=2` 合并成 `/api/orders`）。
  不这样做的话生产环境每个参数组合都会单独占一行。带查询串的完整 URI
  排行在「流量来源与客户端」折叠行里。
- **`host` 保留真实值不做归一化**，这是刻意的：它是安全线索，
  能看出谁在扫什么域名。配套有 `GatewayHostCardinalityHigh` 告警做护栏。
- **「网关开销 vs 后端耗时」是排障第一个该看的面板**：
  两条线差得远说明慢在网关自己，贴在一起说明慢在后端。
"""


# 带级别过滤的选择器。有**两个流选择器**的表达式（错误率这种除法）
# 不能交给 with_level()——它只给第一个 } 后面插，第二个会漏。
# 所以这里先算好，调用时传 level=False。
SELL = with_level(SEL)

BY = "host, method, path"

# 接口使用情况表的三条查询，按 host+method+path 合并成一张表。
# 配方在实验台验证过（截图确认六列齐全、排序和单位都对）。
Q_REQ = 'topk(%d, sum by (%s) (count_over_time(%s [%s])))' % (LIST_N, BY, SEL, RANGE_W)
Q_ERR = ('sum by (%s) (count_over_time(%s | status =~ "[45].." [%s])) / '
         'sum by (%s) (count_over_time(%s [%s])) * 100'
         % (BY, SELL, RANGE_W, BY, SELL, RANGE_W))
Q_P95 = ('quantile_over_time(0.95, %s | unwrap request_time [%s]) by (%s)'
         % (SEL, RANGE_W, BY))

# 顶部「错误率」统计面板：同样是两个流选择器的除法
ERR_RATE = ('sum(count_over_time(%s | status =~ "[45].." [%s])) / '
            'sum(count_over_time(%s [%s])) * 100'
            % (SELL, RANGE_W, SELL, RANGE_W))

LOGS = {"options": {"showTime": True, "showLabels": False,
                    "wrapLogMessage": True, "prettifyLogMessage": True,
                    "sortOrder": "Descending", "enableLogDetails": True,
                    "dedupStrategy": "none"}}


def ts_unit(unit, fill=10):
    """TS 的变体，指定 Y 轴单位。"""
    return {"fieldConfig": {"defaults": {
                "unit": unit,
                "custom": {"lineWidth": 1, "fillOpacity": fill,
                           "showPoints": "never"}}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom",
                                   "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


panels = [
    # ================ 第一屏：RED 三件套 + 业务量 ================
    panel(1, "QPS", "stat", {"h": 4, "w": 5, "x": 0, "y": 0},
          [tgt('sum(rate(%s [%s]))' % (SEL, W), "req/s")],
          "请求速率（RED 的 Rate）。", stat(2)),

    panel(2, "错误率", "stat", {"h": 4, "w": 5, "x": 5, "y": 0},
          [tgt(ERR_RATE, "%", level=False, qtype="instant")],
          "4xx+5xx 占全部请求的百分比（RED 的 Errors）。"
          "阈值按**生产标准**定：1% 转橙、5% 转红，不是按本环境基线。"
          "本测试环境的 demo 应用故意返回约 13.6% 的错误（404/500/503），"
          "所以这里会一直显示红色——那是预期不是故障。",
          stat(1, unit="percent",
               steps=[{"color": "green", "value": None},
                      {"color": "orange", "value": 1},
                      {"color": "red", "value": 5}])),

    panel(3, "P95 响应时间", "stat", {"h": 4, "w": 4, "x": 10, "y": 0},
          [tgt('quantile_over_time(0.95, %s | unwrap request_time [%s]) by ()'
               % (SEL, RANGE_W), "p95")],
          "95 分位响应时间（RED 的 Duration）。用分位数不用平均值："
          "99 个 10ms 加 1 个 10s，平均才 108ms 看着没事，P99 却是 10s。"
          "**必须 by () 聚合**——| json 把每个字段都变成 label，"
          "不写 by 会按全部 label 分组炸成几十条序列（坑位 40）。",
          stat(2, unit="s")),

    panel(4, "传输流量", "stat", {"h": 4, "w": 5, "x": 14, "y": 0},
          [tgt('sum(sum_over_time(%s | unwrap body_bytes_sent [%s]))'
               % (SEL, RANGE_W), "bytes")],
          "时间范围内响应体总字节数，业务量的直观指标。", stat(1, unit="bytes")),

    panel(5, "独立客户端", "stat", {"h": 4, "w": 5, "x": 19, "y": 0},
          [tgt('count(sum by (remote_addr) (count_over_time(%s [%s])))'
               % (SEL, RANGE_W), "个", qtype="instant")],
          "按 remote_addr 去重。注意这是**连接来源 IP**——经过 CDN 或负载均衡时"
          "它是代理的 IP，真实客户端要看 x_forwarded_for 字段。", stat(0)),

    panel(10, "请求趋势（按状态码）", "timeseries", {"h": 6, "w": 16, "x": 0, "y": 4},
          [tgt('sum by (status) (count_over_time(%s [$__interval]))' % SEL,
               "{{status}}")],
          "按状态码堆叠的请求数直方图。分桶用 $__interval（= Grafana 的 step）"
          "而不是速率窗口——后者是滑动窗口，相邻桶重叠会糊成一整块；"
          "配面板级 interval 设最小步长，避免 step 太细变成密集栅栏。",
          dict(BARS, interval="30s")),

    panel(11, "按域名", "table", {"h": 6, "w": 8, "x": 16, "y": 4},
          [tgt('topk(%d, sum by (host) (count_over_time(%s [%s])))'
               % (LIST_N, SEL, RANGE_W), None, qtype="instant")],
          "各业务域名的请求数。生产环境所有域名共用这一个入口，"
          "这里能看出流量构成。host 保留真实值不归一化——它是安全线索，"
          "配套有 GatewayHostCardinalityHigh 告警兜底。",
          table1("请求数", {"host": "域名"}, ["host"])),

    panel(12, "接口使用情况", "table", {"h": 10, "w": 24, "x": 0, "y": 10},
          [tgt(Q_REQ, None, ref="A", qtype="instant"),
           tgt(Q_ERR, None, ref="B", level=False, qtype="instant"),
           tgt(Q_P95, None, ref="C", qtype="instant")],
          "**这张表是整个看板的核心**：一张表同时回答「哪个接口最常用」（业务）"
          "和「哪个接口有问题」（排障）。按请求数倒序，最多 30 行。"
          "接口列已剥掉查询串（?id=1 和 ?id=2 合并），否则生产环境每个参数组合"
          "都会单独占一行。带查询串的完整 URI 排行在「流量来源与客户端」里。"
          "错误率为 0 的接口显示 0 而不是空白。",
          table_endpoints()),

    # ================ 折叠：错误与慢请求 ================
    row(300, "错误与慢请求", 20, collapsed=True, children=[
        panel(20, "网关开销 vs 后端耗时", "timeseries",
              {"h": 8, "w": 12, "x": 0, "y": 21},
              [tgt('quantile_over_time(0.95, %s | unwrap request_time [%s]) by ()'
                   % (SEL, W), "总耗时 P95", ref="A"),
               tgt('quantile_over_time(0.95, %s | upstream_response_time != "" '
                   '| unwrap upstream_response_time [%s]) by ()' % (SEL, W),
                   "后端 P95", ref="B")],
              "**排障第一个该看的面板**：两条线差得远说明慢在网关自己"
              "（连接池、TLS 握手、限流排队），贴在一起说明慢在后端。"
              "upstream_response_time 在请求没走到上游时是空串，所以先过滤非空"
              "再 unwrap，否则 unwrap 会报错。",
              ts_unit("s")),

        panel(21, "错误率趋势（按域名）", "timeseries",
              {"h": 8, "w": 12, "x": 12, "y": 21},
              [tgt('sum by (host) (count_over_time(%s | status =~ "[45].." [%s])) / '
                   'sum by (host) (count_over_time(%s [%s])) * 100'
                   % (SELL, W, SELL, W), "{{host}}", level=False)],
              "哪个域名在出错、什么时候开始的。用比例而不是绝对数："
              "比例能跨域名横向比较，绝对数只反映谁流量大。",
              ts_unit("percent")),

        panel(22, "4xx / 5xx 请求原文", "logs", {"h": 12, "w": 12, "x": 0, "y": 29},
              [tgt('%s | status >= 400' % SEL)],
              "数值比较和下拉用的正则两种写法都能用——| json 把 status 变成字符串"
              "label，但它仍是数字。", LOGS),

        panel(23, "慢请求 (>1s)", "logs", {"h": 12, "w": 12, "x": 12, "y": 29},
              [tgt('%s | request_time > 1' % SEL)],
              "展开单条可看 request_id，拿它到原始日志里查整个请求链路。", LOGS),
    ]),

    # ================ 折叠：流量来源与客户端 ================
    row(400, "流量来源与客户端", 21, collapsed=True, children=[
        panel(30, "Top 来源页 (Referer)", "table", {"h": 8, "w": 8, "x": 0, "y": 22},
              [tgt('topk(10, sum by (referer) (count_over_time(%s [%s])))'
                   % (SEL, RANGE_W), None, qtype="instant")],
              "流量从哪来。直接访问时 referer 是 \"-\" 或空。",
              table1("请求数", {"referer": "来源页"}, ["referer"])),

        panel(31, "Top 客户端类型 (UA)", "table", {"h": 8, "w": 8, "x": 8, "y": 22},
              [tgt('topk(10, sum by (user_agent) (count_over_time(%s [%s])))'
                   % (SEL, RANGE_W), None, qtype="instant")],
              "人还是爬虫、什么浏览器/SDK。UA 基数可能很高，已 topk 封顶。",
              table1("请求数", {"user_agent": "User-Agent"}, ["user_agent"])),

        panel(32, "Top 完整 URI（含查询串）", "table",
              {"h": 8, "w": 8, "x": 16, "y": 22},
              [tgt('topk(10, sum by (uri) (count_over_time(%s [%s])))'
                   % (SEL, RANGE_W), None, qtype="instant")],
              "和第一屏的接口表互补：那张剥掉了查询串，这张保留。"
              "想看「哪些参数组合被用得多」看这里。",
              table1("请求数", {"uri": "URI"}, ["uri"])),
    ]),

    # ================ 折叠：原始日志 ================
    row(500, "原始日志", 22, collapsed=True, children=[
        panel(40, "访问日志（JSON 展开）", "logs", {"h": 16, "w": 24, "x": 0, "y": 23},
              [tgt(SEL)],
              "prettifyLogMessage 开着，JSON 会格式化展开。"
              "点单条左侧箭头可看解析出的全部字段，含 request_id。", LOGS),
    ]),

    # ================ 折叠：说明 ================
    # 放最后：顶层面板必须排在所有 row 之前
    row(600, "说明 / 怎么读这个看板", 23, collapsed=True, children=[
        {"id": 91, "type": "text", "title": "", "transparent": True,
         "gridPos": {"h": 14, "w": 24, "x": 0, "y": 24},
         "options": {"mode": "markdown", "content": DOC}},
    ]),
]

WINDOWS = ["1m", "5m", "10m", "30m", "1h"]

# (下拉显示文字, 实际填进 | status =~ "..." 的正则)
STATUS_OPTS = [
    ("全部", ".+"),
    ("2xx 成功", "2.."),
    ("3xx 重定向", "3.."),
    ("4xx 客户端错误", "4.."),
    ("5xx 服务端错误", "5.."),
    ("4xx + 5xx 全部错误", "[45].."),
]

dashboard = {
    "uid": "loki-gateway-access",
    "title": "Gateway 访问日志 (nginx-gateway-fabric)",
    "description": "nginx-gateway-fabric 数据面的 JSON 访问日志。"
                   "日志格式由 NginxProxy 的 logging.accessLog 定义（escape: json）。",
    "tags": ["loki", "logs", "gateway", "nginx"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 0,
    "refresh": "30s",
    "time": {"from": "now-1h", "to": "now"},
    "editable": True,
    "templating": {"list": [
        # ---- 项目 / 环境：级联链首 ----
        # project 不受任何过滤；environment 只按 project 收窄。
        lv("project", "项目"),

        lv("environment", "环境", '{project=~"$project"}'),

        # 网关数据面 Pod 的 app 标签。NGF 每个 Gateway 会生成一个 <gateway-name>-nginx
        # 的 Deployment，所以用 app=~".+-nginx" 把候选限定在 NGF 数据面。
        #
        # 不加这个限定的话，{source="pod", container="nginx"} 会把 loki、
        # loki-read-gateway、web-gateway 也列进下拉框，而它们是自建 nginx，
        # access log 是普通 combined 格式不是 JSON——本看板的基础选择器带
        # `|~ ^\{ | json`，选中它们会 13 个面板全空。实测这 4 个的 JSON 日志流都是 0。
        #
        # label_name="app" 不能省：变量叫 gateway，但要查的标签是 app。
        lv("gateway", "网关",
           '{%s, source="pod", container="nginx", app=~".+-nginx"}' % PE,
           allv=True, label_name="app"),

        # 这里原本有个 namespace 变量，已移除——见文件上方 BASE 处的说明。

        # host 现在是索引标签（Alloy 侧做了白名单归一化），可以用 label_values
        # 拿到真实取值做下拉。级联自 $gateway，只列当前网关出现过的 host。
        # 下拉里的 _other = 白名单之外的 Host 头（扫描器、配错的客户端），
        # 选它能直接看到这类异常请求。
        lv("host", "Host", '{%s, app=~"$gateway", container="nginx"}' % PE),

        # 状态码过滤。刻意做成【单选】而不是多选：多选时 Grafana 的插值格式
        # 依赖数据源和 format 修饰符，而这里的值本身就是正则（4.. 里的点是元字符），
        # 用 ${status:regex} 会把点转义成 \. 反而失效。单选插值无歧义，
        # 常用的组合（只看错误）直接给一个 [45].. 预设，够用且一定可靠。
        {"name": "status", "label": "状态码", "type": "custom",
         "query": ",".join(f"{t} : {v}" for t, v in STATUS_OPTS),
         "current": {"text": STATUS_OPTS[0][0], "value": STATUS_OPTS[0][1]},
         "options": [{"selected": i == 0, "text": t, "value": v}
                     for i, (t, v) in enumerate(STATUS_OPTS)],
         "description": "按 HTTP 状态码筛选，值是正则。默认 .+ 匹配全部"},

        {"name": "search", "label": "关键字", "type": "textbox",
         "query": "", "current": {"text": "", "value": ""},
         "description": "在日志正文里搜索，留空则不过滤"},

        # 级别：不参与级联。detected_level 不是索引标签，label_values 查不出候选值，
        # 所以是 custom 类型的固定列表，放在 rate_window 之前。
        level_var(),

        {"name": "rate_window", "label": "速率窗口", "type": "interval",
         "query": ",".join(WINDOWS), "auto": False, "refresh": 0,
         "current": {"selected": True, "text": "5m", "value": "5m"},
         "options": [{"selected": w == "5m", "text": w, "value": w} for w in WINDOWS]},
    ]},
    "panels": panels,
}

out = "/root/loki-stack/grafana/dashboard-gateway-access.json"
open(out, "w").write(json.dumps(dashboard, ensure_ascii=False, indent=2))

payload = {"dashboard": dashboard, "overwrite": True,
           "message": "gateway JSON access log 看板"}
r = subprocess.run(
    ["curl", "-s", "--max-time", "30", "-u", AUTH,
     "-H", "Content-Type: application/json",
     "-X", "POST", GRAFANA + "/api/dashboards/db", "-d", json.dumps(payload)],
    capture_output=True, text=True)
print("导入响应:", r.stdout[:250])
