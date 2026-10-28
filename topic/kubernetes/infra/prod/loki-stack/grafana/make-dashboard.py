#!/usr/bin/env python3
"""生成并导入 Grafana 日志总览看板（Loki 数据源）

注意：Grafana 13.2.2 的 Loki 数据源【不会】插值 $__rate_interval，
会把字面量原样发给 Loki，导致：
    parse error: not a valid duration string: "$__rate_interval"
因此这里统一使用自定义的 interval 类型变量 $rate_window，
它由 Grafana 核心插值，保证可用，并且顶部会多一个下拉框可调窗口。
"""
import json, subprocess

DS_UID = "efzm3ndgkowsga"
GRAFANA = "http://10.0.0.101:3000"
AUTH = "admin:admin"

ds = {"type": "loki", "uid": DS_UID}

# 速率/计数窗口统一用这个变量，不要用 $__rate_interval
W = "$rate_window"

# 公共标签过滤：所有采集到的日志都带 project / environment（Alloy 的 stage.static_labels）
# 用 =~ 而非 = ，这样模板变量选 All 时会插值成正则 .+ 也能匹配
SEL = 'project=~"$project", environment=~"$environment"'
JOB = 'job=~"$job"' 



def lv(name, label, stream=None):
    """生成一个 label_values 型模板变量。
    stream 非空时作为流选择器传给 Loki，实现级联收窄。
    type:1 = LABEL_VALUES（Grafana Loki 数据源的变量查询类型）"""
    q = {"label": name, "refId": name, "type": 1}
    if stream:
        q["stream"] = stream
    return {
        "name": name, "label": label, "type": "query", "datasource": ds,
        "query": q,
        "refresh": 2,          # 2 = 时间范围变化时刷新，能跟随上游变量变化
        "includeAll": True, "multi": True, "sort": 1,
        # allValue 必须显式给 ".+"。留空时 Grafana 把 All 插值成 ".*"，
        # 而下游变量的级联查询往往只有这一个 matcher（如 {project=~".*"}），
        # Loki 会直接拒绝：
        #   queries require at least one regexp or equality matcher
        #   that does not have an empty-compatible value
        # ".+" 要求标签存在且非空，语义上正好是「全部」，也和本栈的标签体系配套
        # （系统日志的 namespace=_system、事件的 node=_cluster 这些占位就是为它准备的）。
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


def target(expr, legend=None, qtype="range", ref="A", level=True):
    t = {"refId": ref, "datasource": ds,
         "expr": with_level(expr) if level else expr, "queryType": qtype}
    if legend:
        t["legendFormat"] = legend
    return t


def panel(pid, title, ptype, gp, targets, desc="", extra=None):
    p = {
        "id": pid, "title": title, "type": ptype, "datasource": ds,
        "gridPos": gp, "targets": targets, "description": desc,
    }
    if extra:
        p.update(extra)
    return p


ts_opts = {
    "fieldConfig": {
        "defaults": {
            "custom": {"lineWidth": 1, "fillOpacity": 12, "showPoints": "never"},
            "unit": "short",
        },
        "overrides": [],
    },
    "options": {
        "legend": {"displayMode": "table", "placement": "bottom",
                   "calcs": ["mean", "max"], "showLegend": True},
        "tooltip": {"mode": "multi", "sort": "desc"},
    },
}


def stat_opts(decimals, steps=None, graph="area"):
    fc = {"unit": "short", "decimals": decimals}
    if steps:
        fc["color"] = {"mode": "thresholds"}
        fc["thresholds"] = {"mode": "absolute", "steps": steps}
    return {
        "fieldConfig": {"defaults": fc, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value" if steps else "none",
                    "graphMode": graph},
    }


RANGE_W = "$__range"     # 仪表盘时间范围
LIST_N = 30              # topk 封顶

# 总览的公共选择器：项目/环境 + 顶部的命名空间/节点过滤。
# namespace 用 =~"$namespace" 而非可选：systemd 日志有 namespace=_system 占位，
# 事件有 _cluster，所以 .+ 不会漏掉它们（见坑位 25）。
OV = '{namespace=~"$namespace", node=~"$node", %s}' % SEL

# 错误/致命级别。用 detected_level 而不是正文正则：覆盖 100% 的日志。
ERR_LVL = 'error|critical|fatal'


def row(pid, title, y, collapsed=False, children=None):
    """折叠行。子面板必须嵌在 row["panels"] 里；
    **顶层面板必须排在所有 row 之前**，否则会被算进某个 row 的区段跟着折叠。"""
    return {"id": pid, "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "panels": list(children or [])}


# 柱状堆叠时序：日志量直方图用。配面板级 interval 设最小步长——
# 不设的话 step 可能只有一两秒，画出来是一排密集栅栏。
BARS = {"fieldConfig": {"defaults": {
            "custom": {"drawStyle": "bars", "fillOpacity": 80, "lineWidth": 0,
                       "stacking": {"mode": "normal", "group": "A"}},
            "unit": "short"}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom",
                               "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}}}


def table_noisy():
    """「噪声来源」表：三条查询（条数 / 字节数 / 错误率）按 app+namespace+source 合并。

    Loki 的 instant 查询返回「每序列一个 frame」，标签挂在 field.labels 上
    而不是独立的列——所以必须 labelsToFields 摊成列、merge 并表。
    四个变换顺序不能动；merge 之后值字段带 refId 后缀（Value #A/#B/#C），
    改名和排序都按这个名字，改错了不报错、静默失效。
    """
    idx = {"Time": 0, "app": 1, "namespace": 2, "source": 3,
           "Value #A": 4, "Value #B": 5, "Value #C": 6}
    rn = {"app": "服务", "namespace": "命名空间", "source": "来源",
          "Value #A": "条数", "Value #B": "字节数", "Value #C": "错误率"}
    ov = [
        {"matcher": {"id": "byName", "options": "条数"},
         "properties": [
             {"id": "custom.cellOptions",
              "value": {"type": "gauge", "mode": "gradient",
                        "valueDisplayMode": "text"}},
             {"id": "min", "value": 0},
             # gauge 默认按**整个 frame** 的全局 min/max 刻度，而不是本字段的。
             # 「噪声来源」表里字节数最大 2.5 MiB、条数最大 5970，
             # 不加这行的话条数的 gauge 条是空的（实测截图确认）。
             {"id": "fieldMinMax", "value": True},
             {"id": "color", "value": {"mode": "continuous-BlPu"}},
             {"id": "custom.width", "value": 190}]},
        {"matcher": {"id": "byName", "options": "字节数"},
         "properties": [{"id": "unit", "value": "bytes"},
                        {"id": "decimals", "value": 1},
                        {"id": "custom.width", "value": 120}]},
        {"matcher": {"id": "byName", "options": "错误率"},
         "properties": [
             {"id": "unit", "value": "percent"},
             {"id": "decimals", "value": 1},
             {"id": "noValue", "value": "0"},
             {"id": "color", "value": {"mode": "thresholds"}},
             {"id": "custom.cellOptions",
              "value": {"type": "color-background", "mode": "gradient"}},
             # 阈值按实测基线定：本环境常态约 11%（demo 应用故意造错误），
             # 所以 20% 才转橙、40% 转红。换环境必须重新量，见坑位 19。
             {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                 {"color": "green", "value": None},
                 {"color": "orange", "value": 20},
                 {"color": "red", "value": 40}]}},
             {"id": "custom.width", "value": 110}]},
        {"matcher": {"id": "byName", "options": "命名空间"},
         "properties": [{"id": "custom.width", "value": 160}]},
        {"matcher": {"id": "byName", "options": "来源"},
         "properties": [{"id": "custom.width", "value": 110}]},
    ]
    return {
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "fields": ""}},
        "fieldConfig": {"defaults": {"unit": "short",
                                     "custom": {"align": "auto"}},
                        "overrides": ov},
        "transformations": [
            {"id": "labelsToFields", "options": {"mode": "columns"}},
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": {"Time": True},
                                           "indexByName": idx,
                                           "renameByName": rn}},
            {"id": "sortBy", "options": {"fields": {},
                                         "sort": [{"field": "条数", "desc": True}]}},
        ],
    }


def table1(value_label, renames, order, unit="short"):
    """单查询表格：标签列 + 一个值列。"""
    rn = dict(renames)
    rn["Value #A"] = value_label
    rn["Value"] = value_label
    idx = {"Time": 0}
    for n, f in enumerate(list(order) + ["Value #A", "Value"], start=1):
        idx[f] = n
    return {
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "fields": ""}},
        "fieldConfig": {"defaults": {"unit": unit,
                                     "custom": {"align": "auto"}},
                        "overrides": [
                            {"matcher": {"id": "byName", "options": value_label},
                             "properties": [
                                 {"id": "custom.cellOptions",
                                  "value": {"type": "gauge", "mode": "gradient",
                                            "valueDisplayMode": "text"}},
                                 {"id": "min", "value": 0},
                                 # gauge 默认按**整个 frame** 的全局 min/max
                                 # 刻度，字段量级差得远时条会看不见。
                                 # fieldMinMax 让它按自己的范围刻度。
                                 {"id": "fieldMinMax", "value": True},
                                 {"id": "color",
                                  "value": {"mode": "continuous-BlPu"}}]}]},
        "transformations": [
            {"id": "labelsToFields", "options": {"mode": "columns"}},
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": {"Time": True},
                                           "indexByName": idx,
                                           "renameByName": rn}},
            {"id": "sortBy", "options": {"fields": {},
                                         "sort": [{"field": value_label,
                                                   "desc": True}]}},
        ],
    }


# 「噪声来源」表的三条查询。**整张表不受顶部「级别」下拉影响**：
# 它回答的是「谁在刷日志、谁健康」，按级别收窄之后错误率列会恒为 100%，
# 失去意义。所以三条都传 level=False。
NOISY_A = ('topk(%d, sum by (app, namespace, source) (count_over_time(%s [%s])))'
           % (LIST_N, OV, RANGE_W))
NOISY_B = ('sum by (app, namespace, source) (bytes_over_time(%s [%s]))'
           % (OV, RANGE_W))
NOISY_C = ('sum by (app, namespace, source) (count_over_time(%s '
           '| detected_level=~"%s" [%s])) / '
           'sum by (app, namespace, source) (count_over_time(%s [%s])) * 100'
           % (OV, ERR_LVL, RANGE_W, OV, RANGE_W))

# 全局错误占比：比绝对速率更适合判断异常，因为它不随流量规模变化。
ERR_RATIO = ('sum(count_over_time(%s | detected_level=~"%s" [%s])) / '
             'sum(count_over_time(%s [%s])) * 100'
             % (OV, ERR_LVL, RANGE_W, OV, RANGE_W))

DOC = """\
### 这个看板回答什么

**采集链路健康吗、容量多大、谁在刷日志。**

它是 *运维视角的全局总览*，不是排障工具——定位某个服务的具体问题用
「研发排障 - 实时日志」，看网关流量用「Gateway 访问日志」。

### 第一屏怎么读

| 指标 | 回答 |
|---|---|
| 日志速率 | 写入量级（条/秒） |
| 写入速率 | **容量口径**（字节/秒）——一条访问日志和一条 Java 堆栈差两个数量级 |
| 上报节点数 | **采集链路存活**：低于集群节点数说明有节点的 Alloy 停了或失联 |
| 错误占比 | 全局错误水位。用比例不用绝对速率，因为比例不随流量规模变化 |

往下是日志量直方图（先看几点开始异常）、按命名空间/节点的分布，
再往下是**噪声来源表**——谁在刷日志、谁最占存储。

### 几个容易误解的地方

- **「上报节点数」固定用 1 小时窗口**，不跟顶部的速率窗口。
  空闲的 control-plane 节点 10 分钟内可能一条日志都没有——实测 10 分钟
  窗口里只有 3 个节点出现，用短窗口会误报红色。
- **噪声来源表不受「级别」下拉影响**。它回答「谁在刷、谁健康」，
  按级别收窄后错误率列会恒为 100%，失去意义。
- **Loki 自身的 info 级日志已在采集端丢弃**（见 README「丢弃 Loki 自身的
  噪声日志」）。过滤前它占全栈 84~96%，现在只剩 error/warn。
  想看回全量，注释掉 `config.alloy` 里那一段。
"""


panels = [
    # ================ 第一屏：采集健康 + 容量 ================
    panel(1, "日志速率", "stat", {"h": 4, "w": 6, "x": 0, "y": 0},
          [target('sum(rate(%s [%s]))' % (OV, W), "条/秒")],
          "全部来源（Pod + journal + 事件）的日志写入速率。",
          stat_opts(1, graph="none")),

    panel(2, "写入速率", "stat", {"h": 4, "w": 6, "x": 6, "y": 0},
          [target('sum(bytes_rate(%s [%s]))' % (OV, W), "B/s")],
          "**容量口径**：一条 nginx 访问日志和一条 Java 堆栈差两个数量级，"
          "按条数估容量会严重失真。做容量规划和保留策略时看这个。",
          dict(stat_opts(1, graph="none"),
               fieldConfig={"defaults": {"unit": "Bps", "decimals": 1,
                                         "noValue": "0"}, "overrides": []})),

    # 固定 1h 窗口且统计【全部来源】：空闲的 control-plane 节点 10 分钟内
    # 可能一条日志都没有——实测 10 分钟窗口里只有 3 个节点出现，
    # 用速率窗口会误报红色。这个设计不要动。
    panel(3, "上报节点数 (1h)", "stat", {"h": 4, "w": 6, "x": 12, "y": 0},
          [target('count(count by (node) (count_over_time({node=~".+", ' + SEL + '}[1h])))',
                  "节点")],
          "过去 1 小时有日志上报的节点数（全部来源）。"
          "**低于集群节点总数说明有节点的 Alloy 停了或失联**——"
          "这是本看板唯一的链路存活信号。窗口固定 1h，不跟顶部的速率窗口。",
          stat_opts(0, [{"color": "red", "value": None},
                        {"color": "orange", "value": 4},
                        {"color": "green", "value": 5}], graph="none")),

    panel(4, "错误占比", "stat", {"h": 4, "w": 6, "x": 18, "y": 0},
          [target(ERR_RATIO, "%", level=False, qtype="instant")],
          "error / critical / fatal 占全部日志的百分比。"
          "用比例不用绝对速率——**比例不随流量规模变化**，绝对值会。"
          "阈值按实测基线定：本环境常态约 11%（demo 应用故意造错误），"
          "所以 20% 转橙、40% 转红。换环境必须重新量，见坑位 19。",
          dict(stat_opts(1, [{"color": "green", "value": None},
                             {"color": "orange", "value": 20},
                             {"color": "red", "value": 40}], graph="none"),
               fieldConfig={"defaults": {"unit": "percent", "decimals": 1,
                                         "noValue": "0",
                                         "color": {"mode": "thresholds"},
                                         "thresholds": {"mode": "absolute",
                                                        "steps": [
                                             {"color": "green", "value": None},
                                             {"color": "orange", "value": 20},
                                             {"color": "red", "value": 40}]}},
                            "overrides": []})),

    # 分桶用 $__interval（= Grafana 的 step）而不是速率窗口：后者是滑动窗口，
    # 相邻桶重叠会糊成一整块色带，看不出"几点开始异常"。
    # 配面板级 interval 设最小步长，避免 step 太细变成密集栅栏。
    panel(5, "日志量（按级别）", "timeseries", {"h": 6, "w": 24, "x": 0, "y": 4},
          [target('sum by (detected_level) (count_over_time(%s [$__interval]))' % OV,
                  "{{detected_level}}")],
          "按级别堆叠的日志条数直方图。**先在这里看出几点开始异常，再往下看分布**"
          "——直接看速率曲线容易错过起始时间点。",
          dict(BARS, interval="30s")),

    panel(10, "按命名空间的日志速率", "timeseries",
          {"h": 8, "w": 12, "x": 0, "y": 10},
          [target('sum by (namespace) (rate(%s [%s]))' % (OV, W), "{{namespace}}")],
          "namespace=_system 是宿主机 journal 的占位值，"
          "_cluster 是集群级对象的事件（见坑位 25）。", ts_opts),

    panel(11, "按节点的日志速率", "timeseries",
          {"h": 8, "w": 12, "x": 12, "y": 10},
          [target('sum by (node) (rate(%s [%s]))' % (OV, W), "{{node}}")],
          "某条线掉到 0 说明该节点的 Alloy 可能异常。"
          "注意空闲的 control-plane 节点本来就接近 0，看趋势不看绝对值。",
          ts_opts),

    panel(12, "噪声来源 Top", "table", {"h": 8, "w": 24, "x": 0, "y": 18},
          [target(NOISY_A, None, level=False, qtype="instant"),
           target(NOISY_B, None, ref="B", level=False, qtype="instant"),
           target(NOISY_C, None, ref="C", level=False, qtype="instant")],
          "**谁在刷日志、谁最占存储**——这是总览看板独有的价值。"
          "按条数倒序，最多 30 行。带字节数列是因为运维关心的是存储成本，"
          "不是行数。**整张表不受顶部「级别」下拉影响**：按级别收窄后"
          "错误率列会恒为 100%，失去意义。",
          table_noisy()),

    # ================ 折叠：错误专区 ================
    row(300, "错误专区", 26, collapsed=True, children=[
        panel(20, "错误日志速率（按命名空间）", "timeseries",
              {"h": 8, "w": 12, "x": 0, "y": 27},
              [target('sum by (namespace) (rate(%s | detected_level=~"%s" [%s]))'
                      % (OV, ERR_LVL, W), "{{namespace}}", level=False)],
              "用 detected_level 而不是正文正则匹配 error——"
              "后者会把「提到 error 这个词」的行也算进来（坑位 63 同类）。",
              ts_opts),

        panel(21, "kubelet 错误速率（按节点）", "timeseries",
              {"h": 8, "w": 12, "x": 12, "y": 27},
              [target('sum by (node) (rate({unit="kubelet.service", ' + SEL + '} '
                      '|~ `(?i)(^|[^a-z])(error|failed)([^a-z]|$)` [%s]))' % W,
                      "{{node}}")],
              "kubelet 报错是节点级问题的早期信号。", ts_opts),

        panel(22, "错误日志流", "logs", {"h": 12, "w": 24, "x": 0, "y": 35},
              [target('%s | detected_level=~"%s" '
                      '!~ `caller=(metrics|roundtrip|engine)\\.go`'
                      % (OV, ERR_LVL), level=False)],
              "末尾的 !~ 剔除 Loki 自身 querier/ruler 把查询语句原样打进日志的行——"
              "那些行里含 error 字样，会淹没真实错误。",
              {"options": {"showTime": True, "showLabels": True,
                           "wrapLogMessage": True, "sortOrder": "Descending",
                           "enableLogDetails": True, "dedupStrategy": "none"}}),
    ]),

    # ================ 折叠：系统日志 ================
    row(400, "系统日志（宿主机 journal）", 27, collapsed=True, children=[
        panel(30, "systemd unit Top", "table", {"h": 8, "w": 24, "x": 0, "y": 28},
              [target('topk(%d, sum by (unit, node) (count_over_time('
                      '{job="systemd-journal", ' % LIST_N + SEL + '}[%s])))' % RANGE_W,
                      None, qtype="instant")],
              "宿主机 systemd 服务日志，含 kubelet / containerd / sshd 等。"
              "**注意 session-*.scope**：每次 SSH 登录 systemd 都新建一个 scope，"
              "会变成一个新的 unit 取值且只增不减（见 README 容量一节）。",
              table1("条数", {"unit": "服务单元", "node": "节点"},
                     ["unit", "node"])),
    ]),

    # ================ 折叠：实时日志流 ================
    row(500, "实时日志流", 28, collapsed=True, children=[
        panel(40, "实时日志流", "logs", {"h": 14, "w": 24, "x": 0, "y": 29},
              [target('{namespace=~"$namespace", node=~"$node", ' + JOB + ', '
                      + SEL + '}')],
              "受顶部全部过滤器约束。定位某个服务的具体问题用"
              "「研发排障 - 实时日志」看板，那里的日志流是主角、过滤器也更合用。",
              {"options": {"showTime": True, "showLabels": False,
                           "wrapLogMessage": True, "sortOrder": "Descending",
                           "enableLogDetails": True, "dedupStrategy": "none"}}),
    ]),

    # ================ 折叠：说明 ================
    row(600, "说明 / 怎么读这个看板", 29, collapsed=True, children=[
        {"id": 91, "type": "text", "title": "", "transparent": True,
         "gridPos": {"h": 14, "w": 24, "x": 0, "y": 30},
         "options": {"mode": "markdown", "content": DOC}},
    ]),
]

WINDOWS = ["1m", "5m", "10m", "30m", "1h"]

dashboard = {
    "uid": "loki-log-overview",
    "title": "日志总览 (Loki)",
    "description": "Kubernetes Pod 日志 + 宿主机 systemd journal 总览。"
                   "数据来源 Alloy -> Loki（租户 jasper）。",
    "tags": ["loki", "logs", "alloy"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 0,
    "refresh": "30s",
    "time": {"from": "now-1h", "to": "now"},
    "editable": True,
    "templating": {"list": [
        # ---- 级联筛选 ----
        # 每个变量的 stream 选择器只引用「排在它前面」的变量，
        # 选了上游就自动收窄下游候选值。顺序不能乱，否则引用到未解析的变量。
        lv("project", "项目"),

        lv("environment", "环境",
           '{project=~"$project"}'),

        lv("namespace", "命名空间",
           '{project=~"$project", environment=~"$environment"}'),

        lv("node", "节点",
           '{project=~"$project", environment=~"$environment", namespace=~"$namespace"}'),

        lv("job", "job",
           '{project=~"$project", environment=~"$environment", '
           'namespace=~"$namespace", node=~"$node"}'),
        # 速率窗口：必须用自定义 interval 变量，不能用 $__rate_interval（见文件头说明）
        # 放在链尾：它不参与级联，而「项目/环境」才是最高层筛选，应该排在最左
        # 级别：不参与级联。detected_level 不是索引标签，label_values 查不出候选值，
        # 所以是 custom 类型的固定列表，放在 rate_window 之前。
        level_var(),

        {"name": "rate_window", "label": "速率窗口", "type": "interval",
         "query": ",".join(WINDOWS), "auto": False, "auto_count": 30,
         "auto_min": "10s", "refresh": 0, "skipUrlSync": False,
         "current": {"selected": True, "text": "5m", "value": "5m"},
         "options": [{"selected": w == "5m", "text": w, "value": w} for w in WINDOWS]},
    ]},
    "panels": panels,
}

out_path = "/root/loki-stack/grafana/dashboard-log-overview.json"
open(out_path, "w").write(json.dumps(dashboard, ensure_ascii=False, indent=2))

# folderUid 必须显式给：不给的话 Grafana 会把看板放回 General
# （实测 payload 不带这个字段，返回的 folderUid 是空串），
# 文件夹级的权限配置就随之失效。看板归属属于部署配置，
# 和看板内容一样应当由脚本持有，不靠手工拖拽维持。
payload = {"dashboard": dashboard, "folderUid": "ops-only",
           "overwrite": True,
           "message": "fix: $__rate_interval 不被 Loki 数据源插值，改用 $rate_window"}

r = subprocess.run(
    ["curl", "-s", "--max-time", "30", "-u", AUTH,
     "-H", "Content-Type: application/json",
     "-X", "POST", GRAFANA + "/api/dashboards/db", "-d", json.dumps(payload)],
    capture_output=True, text=True)
print("导入响应:", r.stdout[:250])
