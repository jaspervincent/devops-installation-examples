#!/usr/bin/env python3
"""研发排障看板：实时日志流 + Java/Nginx 专项分析

设计取向：给研发「快速定位自己服务的问题」用，不是给运维看全局容量的。
所以顶部过滤器按 研发的思考顺序 排列：哪个服务 -> 什么级别 -> 搜什么关键字。

注意：Grafana 的 Loki 数据源不插值 $__rate_interval（会原样发给 Loki 并报
parse error），所以窗口一律用自定义 interval 变量 $rate_window。
"""
import json, subprocess

DS_UID = "efzm3ndgkowsga"
GRAFANA = "http://10.0.0.101:3000"
AUTH = "admin:admin"

ds = {"type": "loki", "uid": DS_UID}
W = "$rate_window"

# 公共流选择器：研发按 服务(app) 过滤，而不是带 ReplicaSet 哈希的 pod 名
# source 区分应用容器日志(pod)与宿主机系统日志(systemd)。
# kubelet/containerd 这类系统服务也有 app 标签（unit 名去掉 .service），
# 所以「服务」下拉框里能直接选到 kubelet。
# ---- 项目 / 环境 ----
# 这两个是最高层级的筛选，排在级联链首。目前各只有一个取值
# （project=jp / environment=test），但链路先铺好，
# 将来接入第二个项目或环境时，看板不用再动。
PE = 'project=~"$project", environment=~"$environment"'

# ---- 「范围」：默认排除基础设施命名空间 ----
# 实测近 6h：loki 自身 496k 条，而 order-service 21k、web-gateway 21k——
# 默认 All 时开发者看自己的服务会被基础设施日志淹没（占 96%）。
#
# 做成 namespace!~"$scope" 而不是正向匹配，是因为 **Loki 的 RE2 不支持
# 负向先行断言**，写不出「除了 loki 之外」的正向正则。
# 「全部」对应 ^$：所有真实命名空间都非空，所以等于不排除。
# 正则全部锚定，否则 namespace!~"loki" 会连 "myloki" 一起排掉。
SCOPES = [
    ("业务服务", "^(loki|kube-system|alloy|local-path-storage|_system)$"),
    ("含系统日志", "^(loki|kube-system|alloy|local-path-storage)$"),
    ("全部", "^$"),
]


def scope_var():
    """单选。**不能做成多选**：多选时 Grafana 的插值会把正则里的 . 转义掉，
    正则反而失效（README 坑位，Gateway 的状态码下拉踩过）。"""
    return {
        "name": "scope", "label": "范围", "type": "custom",
        "query": ", ".join("%s : %s" % (t, v) for t, v in SCOPES),
        "options": [{"selected": i == 0, "text": t, "value": v}
                    for i, (t, v) in enumerate(SCOPES)],
        "current": {"selected": True, "text": SCOPES[0][0], "value": SCOPES[0][1]},
        "multi": False, "includeAll": False, "refresh": 0, "skipUrlSync": False,
        "description": "默认「业务服务」排除 loki / kube-system / alloy 等基础设施"
                       "命名空间和宿主机日志——它们占全部日志的 96%，"
                       "不排掉的话开发者找自己服务的日志像大海捞针。"
                       "要看基础设施切「含系统日志」或「全部」。",
    }


SEL = ('{%s, source=~"$source", namespace=~"$namespace", '
       'namespace!~"$scope", app=~"$app"}' % PE)
# 自由搜索：变量为空时 |= "" 匹配全部，实测可用
SEARCH = '|= "$search"'
# nginx combined 格式解析。
#
# **必须跟上 | status != ""**：LogQL 的 pattern 解析器对不匹配的行
# 不会丢弃，只是不给它们打标签。不过滤的话所有非 nginx 日志会归进一个
# 空标签序列——实测那条序列 34.7/s，而真实 nginx 数据只有 0.1~0.44/s，
# 图上完全看不见（占 1.3%）。
NGINX_PATTERN = ('| pattern `<ip> - <_> [<_>] "<method> <path> <_>" '
                 '<status> <size> <_> "<ua>" <rt>`')
NGINX = SEL + " " + NGINX_PATTERN + ' | status != ""'
ERR_RE = '`(?i)(^|[^a-z])(error|fatal|panic|exception)([^a-z]|$)`'



def lv(name, label, stream=None):
    """生成一个 label_values 型模板变量。
    stream 非空时作为流选择器传给 Loki，实现级联收窄。"""
    q = {"label": name, "refId": name, "type": 1}
    if stream:
        q["stream"] = stream
    return {
        "name": name, "label": label, "type": "query", "datasource": ds,
        "query": q,
        "refresh": 2,          # 2 = 时间范围变化时刷新，能跟随上游变量变化
        "includeAll": True, "multi": True, "sort": 1,
        # allValue 必须显式给 ".+"。留空时 Grafana 把 All 插值成 ".*"，
        # 而下游变量的级联查询往往只有这一个 matcher（如 {source=~".*"}），
        # Loki 会直接拒绝：
        #   queries require at least one regexp or equality matcher
        #   that does not have an empty-compatible value
        # ".+" 要求标签存在且非空，语义上正好是「全部」。
        "allValue": ".+",
        "current": {"text": ["All"], "value": ["$__all"]},
    }


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


# ---- 「列表」表格面板 ----
# 统计面板只有一个数字，这个把具体名字和条数列出来，受顶部全部过滤器约束。
#
# 窗口用 $__range（仪表盘时间范围）而不是 $rate_window：
# 用户把时间选成 30 分钟，期望的就是「这 30 分钟里有哪些」。
# 实测本 Grafana 的 Loki 数据源**会**插值 $__range——注意这跟
# $__rate_interval 不一样，后者不被插值（见文件头）。旁边的统计面板
# 也改成了同一个窗口，两者才对得上。
RANGE_W = "$__range"
LIST_H = 8          # 列表面板高度；插入后其后所有面板下移这么多
LIST_N = 30         # topk 封顶。不封顶的话批量重启这类场景会刷出几百行


def list_table(value_label, renames, order):
    """表格面板的 options / fieldConfig / transformations。

    **不能用 bargauge**：实测它只画出一个无名的条，显示最后一个序列的值。
    根因是 Loki 的 instant 查询返回「每序列一个 frame」，字段只有
    Time/Value，标签挂在 field.labels 上而不是独立的列。

    所以必须 labelsToFields 把标签摊成列、merge 把多个 frame 并成一张表。
    四个变换的顺序不能动：
        labelsToFields -> merge -> organize -> sortBy

    另一个坑：merge 之后值字段叫 "Value #A"（带 refId 后缀），不是 "Value"。
    organize 的改名要按这个名字来，改错了不报错——列名保持英文、
    后面的 sortBy 也跟着静默失效。两个名字都写上以防版本差异。
    """
    rn = dict(renames)
    rn["Value #A"] = value_label
    rn["Value"] = value_label
    # 列顺序：labelsToFields 摊出来的列是标签名字母序的，这里显式排。
    # indexByName 用的是**改名前**的字段名。
    idx = {"Time": 0}
    for n, f in enumerate(list(order) + ["Value #A", "Value"], start=1):
        idx[f] = n
    return {
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "fields": ""}},
        "fieldConfig": {"defaults": {"unit": "short", "custom": {"align": "auto"}},
                        "overrides": []},
        "transformations": [
            {"id": "labelsToFields", "options": {"mode": "columns"}},
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": {"Time": True},
                                           "indexByName": idx,
                                           "renameByName": rn}},
            {"id": "sortBy", "options": {"fields": {},
                                         "sort": [{"field": value_label, "desc": True}]}},
        ],
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


LOGS_OPTS = {"options": {
    "showTime": True, "showLabels": False, "wrapLogMessage": True,
    "prettifyLogMessage": False, "sortOrder": "Descending",
    "enableLogDetails": True, "dedupStrategy": "none",
}}

TS_OPTS = {
    "fieldConfig": {"defaults": {
        "custom": {"lineWidth": 1, "fillOpacity": 15, "showPoints": "never",
                   "stacking": {"mode": "normal"}},
        "unit": "short"}, "overrides": []},
    "options": {"legend": {"displayMode": "list", "placement": "bottom",
                           "showLegend": True},
                "tooltip": {"mode": "multi", "sort": "desc"}},
}


def stat(decimals, steps=None, unit="short", novalue="0"):
    """noValue：查询无结果时显示什么，默认 "0" 而不是 Grafana 的 "No data"。
    「这段时间没有错误」和「采集断了」语义完全不同，No data 把两者混成一样。"""
    fc = {"unit": unit, "decimals": decimals, "noValue": novalue}
    if steps:
        fc["color"] = {"mode": "thresholds"}
        fc["thresholds"] = {"mode": "absolute", "steps": steps}
    return {"fieldConfig": {"defaults": fc, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"],
                                          "fields": "", "values": False},
                        "colorMode": "value" if steps else "none",
                        "graphMode": "area"}}


# 柱状堆叠时序：日志量直方图用。
#
# 配合面板级 interval="30s"（最小步长）。不设的话 Grafana 对 30m 范围
# 算出约 1 秒的 step，每桶只有 1~4 条日志，画出来是一排密集栅栏，
# 基线部分完全读不出趋势——实测截图确认过。
#紧贴日志流上方是刻意的——
# Kibana / Grafana Explore / Datadog 都是这个组合：先在直方图上看出
# 「几点开始异常」，再往下读日志。它按级别堆叠，顺带取代了原来的饼图。
BARS = {"fieldConfig": {"defaults": {
            "custom": {"drawStyle": "bars", "fillOpacity": 80, "lineWidth": 0,
                       "stacking": {"mode": "normal", "group": "A"}},
            "unit": "short"}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom",
                               "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}}}


DOC = """\
### 这个看板回答什么

**我的服务现在在报什么、从什么时候开始的。**

顶部选服务 → 看直方图确定时间点 → 在日志流里读原文。

### 顶部过滤器

| 变量 | 说明 |
|---|---|
| 范围 | 默认「业务服务」，**排除 loki / kube-system / alloy 和宿主机日志** |
| 来源 | `pod` = 应用容器，`systemd` = 宿主机服务（kubelet / containerd 等） |
| 服务 | 容器取 Pod 的 `app` 标签；系统服务取 unit 名去掉 `.service` |
| 级别 | 筛的是 `detected_level`（Loki 推断，覆盖 100%），不是索引标签 `level` |
| 关键字 | 在日志正文里搜，留空不过滤。例：`orderId=12345` |

### 为什么默认排除基础设施

实测近 6 小时：`loki` 自身 49.6 万条，而 `order-service` 2.1 万、
`web-gateway` 2.1 万——**基础设施日志占 96%**。不排掉的话，
找自己服务的日志像大海捞针。要看基础设施，把「范围」切成「全部」。

### 两个容易误解的地方

- **Java 异常堆栈已由 Alloy 合并成单条记录**，展开能看到完整调用栈，
  不会被拆成几十条。
- **Nginx 专区需要选中 nginx 类服务**（如 `web-gateway`）。查询带
  `| status != ""`，非 nginx 格式的日志会被自然过滤掉，所以选错服务
  是空面板而不是一堆噪声。
"""


def row(pid, title, y, collapsed=False, children=None):
    """折叠行。

    **子面板必须嵌在 row["panels"] 里，不能留在顶层 panels 列表。**
    另外 **顶层面板必须排在所有 row 之前**——排在某个 row 之后的顶层面板
    会被算作属于该 row 的区段，行一折叠就跟着全不显示（事件看板踩过，
    只有整屏截图才能发现）。
    """
    return {"id": pid, "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "panels": list(children or [])}


# 「错误占比」有**两个**流选择器，而 with_level() 只给第一个加级别过滤
# （它的 docstring 写明了"本栈每条表达式只有一个流选择器"）。
# 所以这里手工把 LVL 拼进两边，调用时传 level=False 让 tgt() 别再插一次。
ERR_RATIO = ('sum(count_over_time(%s %s %s |~ %s [%s])) / '
             'sum(count_over_time(%s %s %s [%s])) * 100'
             % (SEL, LVL, SEARCH, ERR_RE, RANGE_W,
                SEL, LVL, SEARCH, RANGE_W))


panels = [
    # ================ 第一屏 ================
    panel(1, "日志速率", "stat", {"h": 4, "w": 6, "x": 0, "y": 0},
          [tgt('sum(rate(%s %s [%s]))' % (SEL, SEARCH, W), "条/秒")],
          "当前筛选条件下的日志速率。受「范围」影响——默认不含基础设施日志。",
          stat(1)),

    panel(2, "错误数", "stat", {"h": 4, "w": 6, "x": 6, "y": 0},
          [tgt('sum(count_over_time(%s %s |~ %s [%s]))'
               % (SEL, SEARCH, ERR_RE, RANGE_W), "条", qtype="instant")],
          "仪表盘时间范围内含 error / fatal / panic / exception 的日志条数。"
          "刻意不做阈值着色：这个数随时间范围线性变化，固定阈值在改范围时必然失真。"
          "判断是否异常看右边的「错误占比」。",
          stat(0)),

    panel(3, "错误占比", "stat", {"h": 4, "w": 6, "x": 12, "y": 0},
          [tgt(ERR_RATIO, "%", level=False, qtype="instant")],
          "错误日志占全部日志的百分比。比「错误数」更适合判断是否异常——"
          "比例不随时间范围变化，而计数会。"
          "阈值按实测基线定：本环境常态 9.9~10.1%（5m/30m/1h/6h 四个窗口都一样，"
          "demo 应用按固定比例造错误），所以 20% 才转橙、40% 转红。"
          "**换到真实环境必须重新量基线再定**，见坑位 19。",
          stat(1, [{"color": "green", "value": None},
                   {"color": "orange", "value": 20},
                   {"color": "red", "value": 40}], unit="percent")),

    panel(4, "涉及服务数", "stat", {"h": 4, "w": 6, "x": 18, "y": 0},
          [tgt('count(sum by (app) (count_over_time(%s %s [%s])))'
               % (SEL, SEARCH, RANGE_W), "个", qtype="instant")],
          "仪表盘时间范围内出现过日志的服务数量。"
          "具体是哪些服务，展开「服务与级别分布」折叠行。",
          stat(0)),

    # 分桶宽度用 $__interval（= Grafana 的 step）而不是 $rate_window：
    # 速率窗口是**滑动**窗口，相邻桶大幅重叠，画出来是一整块实心色带，
    # 看不出"几点开始异常"——而那正是这个面板存在的理由。
    # $__interval 让桶与桶严格相接，才是真正的直方图。
    # 代价：这个面板不受顶部「速率窗口」下拉影响（它的粒度跟随视图宽度）。
    panel(5, "日志量（按级别）", "timeseries", {"h": 5, "w": 24, "x": 0, "y": 4},
          [tgt('sum by (detected_level) (count_over_time(%s %s [$__interval]))'
               % (SEL, SEARCH), "{{detected_level}}")],
          "按级别堆叠的日志条数直方图，分桶宽度自动跟随视图（$__interval）。"
          "先在这里看出几点开始异常，再往下读日志——这是读日志的标准顺序，"
          "直接翻日志流很容易错过起始时间点。"
          "不受顶部「速率窗口」影响：那是滑动窗口，会把直方图糊成一整块。",
          dict(BARS, interval="30s")),

    panel(10, "实时日志流", "logs", {"h": 18, "w": 24, "x": 0, "y": 9},
          [tgt('%s %s' % (SEL, SEARCH))],
          "顶部选服务、选级别、填关键字即可。"
          "Java 异常堆栈已由 Alloy 合并为单条记录，展开可看完整调用栈。"
          "点左侧时间戳展开标签详情，里面的字段可直接点击做过滤。",
          LOGS_OPTS),

    # ================ 折叠：错误专区 ================
    row(300, "错误专区", 27, collapsed=True, children=[
        panel(20, "错误日志流", "logs", {"h": 14, "w": 16, "x": 0, "y": 28},
              [tgt('%s %s |~ %s' % (SEL, SEARCH, ERR_RE))],
              "按日志正文匹配 error / fatal / panic / exception。"
              "和顶部「级别」下拉是两条不同的路子：级别走 detected_level"
              "（Loki 服务端推断），这里走正文正则，两者可以叠加。",
              LOGS_OPTS),

        panel(21, "错误速率（按服务）", "timeseries", {"h": 7, "w": 8, "x": 16, "y": 28},
              [tgt('sum by (app) (rate(%s %s |~ %s [%s]))' % (SEL, SEARCH, ERR_RE, W),
                   "{{app}}")],
              "哪个服务在报错、什么时候开始的", TS_OPTS),

        panel(22, "Java 异常类型 Top 10", "timeseries", {"h": 7, "w": 8, "x": 16, "y": 35},
              [tgt('topk(10, sum by (exception) (count_over_time(%s '
                   '|~ `Exception|Error` '
                   '| regexp `(?P<exception>[\\w.]+(?:Exception|Error))` '
                   '| exception != "" [%s])))' % (SEL, W), "{{exception}}")],
              "从日志里正则提取异常类名后统计。可快速看出主要故障类型", TS_OPTS),
    ]),

    # ================ 折叠：服务与级别分布 ================
    row(500, "服务与级别分布", 28, collapsed=True, children=[
        panel(30, "涉及服务列表", "table", {"h": 8, "w": 16, "x": 0, "y": 29},
              [tgt('topk(%d, sum by (app, namespace, source) (count_over_time(%s %s [%s])))'
                   % (LIST_N, SEL, SEARCH, RANGE_W), None, qtype="instant")],
              "命中的服务，按日志条数倒序，最多 30 行。"
              "带上 source 是因为同一个「服务」下拉里混着应用容器和宿主机 unit，"
              "只看名字分不清 kubelet 是哪一种。",
              list_table("日志条数", {"app": "服务", "namespace": "命名空间",
                                      "source": "来源"},
                         ["app", "namespace", "source"])),

        panel(31, "按级别分布", "piechart", {"h": 8, "w": 8, "x": 16, "y": 29},
              [tgt('sum by (detected_level) (count_over_time(%s %s [%s]))'
                   % (SEL, SEARCH, RANGE_W), "{{detected_level}}")],
              "整个时间范围的级别构成。随时间怎么变看第一屏的直方图。",
              {"options": {"legend": {"displayMode": "list", "placement": "right"},
                           "reduceOptions": {"calcs": ["lastNotNull"]}}}),
    ]),

    # ================ 折叠：Nginx ================
    row(400, "Nginx 访问日志分析", 29, collapsed=True, children=[
        panel(40, "HTTP 状态码分布", "timeseries", {"h": 8, "w": 8, "x": 0, "y": 30},
              [tgt('sum by (status) (rate(%s %s [%s]))' % (NGINX, SEARCH, W),
                   "{{status}}")],
              "用 LogQL 的 pattern 解析器解析 combined 格式。"
              "选非 nginx 服务时这里是空面板（被 status 非空过滤掉），"
              "而不是一条压扁一切的噪声线——后者是修复之前的行为，见 NGINX 的注释。",
              TS_OPTS),

        panel(41, "5xx 错误请求", "logs", {"h": 8, "w": 16, "x": 8, "y": 30},
              [tgt('%s %s | status =~ "5.."' % (NGINX, SEARCH))],
              "直接列出所有 5xx 请求原文", LOGS_OPTS),

        panel(42, "访问量 Top 10 路径", "timeseries", {"h": 8, "w": 12, "x": 0, "y": 38},
              [tgt('topk(10, sum by (path) (rate(%s %s [%s])))' % (NGINX, SEARCH, W),
                   "{{path}}")],
              "哪些接口被访问得最多", TS_OPTS),

        panel(43, "错误率 Top 10 路径", "timeseries", {"h": 8, "w": 12, "x": 12, "y": 38},
              [tgt('topk(10, sum by (path) (rate(%s %s | status =~ "[45].." [%s])))'
                   % (NGINX, SEARCH, W), "{{path}}")],
              "只统计 4xx/5xx，定位有问题的接口", TS_OPTS),
    ]),

    # ================ 折叠：说明 ================
    # 放最后：顶层面板必须排在所有 row 之前（见 row() 的注释）
    row(600, "说明 / 怎么读这个看板", 30, collapsed=True, children=[
        {"id": 91, "type": "text", "title": "", "transparent": True,
         "gridPos": {"h": 14, "w": 24, "x": 0, "y": 31},
         "options": {"mode": "markdown", "content": DOC}},
    ]),
]

WINDOWS = ["1m", "5m", "10m", "30m", "1h"]


dashboard = {
    "uid": "loki-dev-troubleshoot",
    "title": "研发排障 - 实时日志",
    "description": "给研发快速定位服务问题用：选服务 -> 看直方图定位时间点 -> 读日志流。"
                   "默认「范围=业务服务」，已排除 loki/kube-system 等基础设施日志"
                   "（它们占全部日志的 96%）。Java 异常堆栈已在采集端合并为单条记录。"
                   "系统服务(kubelet/containerd 等)把范围切成「含系统日志」。",
    "tags": ["loki", "logs", "dev", "troubleshoot"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 0,
    "refresh": "10s",
    "time": {"from": "now-30m", "to": "now"},
    "editable": True,
    "templating": {"list": [
        # ---- 级联筛选 ----
        # 每个变量的 stream 选择器引用「排在它前面」的变量，
        # 这样选了上游就自动收窄下游的候选值。
        # 例：日志来源选 systemd 后，服务下拉框只剩 kubelet/containerd 等 8 个，
        #     而不是混着应用服务的 22 个。
        # type:1 = LABEL_VALUES（Grafana Loki 数据源的变量查询类型）

        # ---- 项目 / 环境：级联链首 ----
        # project 不受任何过滤；environment 只按 project 收窄。
        lv("project", "项目"),

        lv("environment", "环境", '{project=~"$project"}'),

        # 范围：必须排在 namespace / app **之前**——它们的候选值查询引用
        # $scope，而引用排在自己后面的变量会拿不到值（见本文件末尾的说明）。
        scope_var(),

        lv("source", "日志来源", '{%s}' % PE),

        # 候选值也受「范围」收窄：选「业务服务」时下拉里不该再出现 loki。
        lv("namespace", "命名空间",
           '{%s, source=~"$source", namespace!~"$scope"}' % PE),

        # 研发最常用：按服务过滤。app 标签来自 Pod 的 app / app.kubernetes.io/name；
        # 系统服务则是 unit 名去掉 .service
        lv("app", "服务",
           '{%s, source=~"$source", namespace=~"$namespace", '
           'namespace!~"$scope"}' % PE),

        # 自由文本搜索：留空时 |= "" 匹配全部
        {"name": "search", "label": "关键字", "type": "textbox",
         "query": "", "current": {"text": "", "value": ""},
         "description": "在日志正文里搜索，留空则不过滤。例：orderId=123"},

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

out = "/root/loki-stack/grafana/dashboard-dev-troubleshoot.json"
open(out, "w").write(json.dumps(dashboard, ensure_ascii=False, indent=2))

# folderUid 必须显式给：不给的话 Grafana 会把看板放回 General
# （实测 payload 不带这个字段，返回的 folderUid 是空串），
# 文件夹级的权限配置就随之失效。看板归属属于部署配置，
# 和看板内容一样应当由脚本持有，不靠手工拖拽维持。
payload = {"dashboard": dashboard, "folderUid": "dev-visible",
           "overwrite": True,
           "message": "研发排障看板"}
r = subprocess.run(
    ["curl", "-s", "--max-time", "30", "-u", AUTH,
     "-H", "Content-Type: application/json",
     "-X", "POST", GRAFANA + "/api/dashboards/db", "-d", json.dumps(payload)],
    capture_output=True, text=True)
print("导入响应:", r.stdout[:250])
