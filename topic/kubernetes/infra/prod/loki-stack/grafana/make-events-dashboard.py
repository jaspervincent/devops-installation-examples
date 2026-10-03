#!/usr/bin/env python3
"""Kubernetes 事件看板

数据来自 alloy-events（Deployment 单副本，见 README 步骤 4b），
标签 source=kubernetes-events，日志行是 logfmt。

两个设计要点：

 1. **默认时间窗 30m，和别的看板一致。**
    这里有个取舍。本测试环境事件极稀疏（实测 24 小时才三百条，空闲时段为 0），
    用 30m 打开经常是空的——但**不能按这个来定默认值**：真实集群的事件量
    大几个数量级，默认就扫 24h 会让每次打开都拉一大批数据，把 Loki 压垮。

    所以默认取 30m，稀疏环境下看到空面板是预期的，往上调范围即可。
    apiserver 没设 --event-ttl，事件在 etcd 里只留 1 小时——Loki 里的保留
    由 retention_stream 管（source=kubernetes-events 为 168h），拉长范围能查到。

 2. **reason / kind 是 logfmt 解析出的字段，不是索引标签**，拿不到 label_values，
    所以用 textbox 填正则。这是刻意的：reason 有几十种取值，做成索引标签会把
    stream 数抬高一个量级，而这套栈真正的风险是标签基数不是数据量。

Grafana 的 Loki 数据源不插值 $__rate_interval，窗口统一用 $rate_window。
"""
import json, subprocess

DS_UID = "efzm3ndgkowsga"
GRAFANA = "http://10.0.0.101:3000"
AUTH = "admin:admin"

ds = {"type": "loki", "uid": DS_UID}
W = "$rate_window"

# 流选择器。source 是固定的非空 matcher，所以这里天然不会踩
# "empty-compatible value" 那个坑（见 README 坑位）。
# ---- 项目 / 环境 ----
# 这两个是最高层级的筛选，排在级联链首。目前各只有一个取值
# （project=jp / environment=test），但链路先铺好，
# 将来接入第二个项目或环境时，看板不用再动。
PE = 'project=~"$project", environment=~"$environment"'

STREAM = ('{%s, source="kubernetes-events", '
          'namespace=~"$namespace", level=~"$level"}' % PE)

# 顶部过滤全部叠加在这上面
SEL = STREAM + ' | logfmt | reason =~ "$reason" | kind =~ "$kind" |= "$search"'

# 只看 Warning（事件的 type=Warning 在采集时映射成了 level=warn）
WARN = ('{%s, source="kubernetes-events", namespace=~"$namespace", level="warn"}'
        ' | logfmt | reason =~ "$reason" | kind =~ "$kind" |= "$search"' % PE)

# 趋势面板用：带 reason/kind/search 过滤，但**不带 level**——
# 它本身就是按 level 分组展示 Normal/Warning 的构成，
# 再被顶部「级别」下拉收窄就只剩一种颜色，面板失去意义。
TREND = ('{%s, source="kubernetes-events", namespace=~"$namespace"}'
         ' | logfmt | reason =~ "$reason" | kind =~ "$kind" |= "$search"' % PE)


def lv(name, label, stream=None, desc=""):
    """label_values 型模板变量，级联收窄。type:1 = LABEL_VALUES"""
    q = {"label": name, "refId": name, "type": 1}
    if stream:
        q["stream"] = stream
    return {"name": name, "label": label, "type": "query", "datasource": ds,
            "query": q, "refresh": 2, "includeAll": True, "multi": True,
            # 见其他三个看板里的同名注释：留空会被 Grafana 插值成 ".*"，
            # 下游只有一个 matcher 的级联查询会被 Loki 拒绝。
            "allValue": ".+",
            "sort": 1, "description": desc,
            "current": {"text": ["All"], "value": ["$__all"]}}


def tb(name, label, default, desc):
    return {"name": name, "label": label, "type": "textbox", "query": default,
            "current": {"text": default, "value": default}, "description": desc}


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


# 事件流的行重排模板。事件是 logfmt，原始行里 objectAPIversion / objectRV /
# eventRV / reportinginstance / sourcecomponent 这些字段对人没用，却占掉一半
# 行宽，把真正要看的 name / reason / msg 挤到行尾。
# 原始字段仍可展开查看（面板开着 enableLogDetails），只是不再占据行宽。
LINE_FMT = ('| line_format `{{.reason}}  {{.kind}}/{{.name}}  '
            '[{{.namespace}}]  {{.msg}}`')


def list_table(value_label, renames, order, gauge=False):
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
                        # gauge 单元格必须配 fieldMinMax：Grafana 默认按整个
                        # frame 的全局 min/max 刻度，量级差得远时条会看不见
                        # （见坑位 64）。
                        "overrides": [
                            {"matcher": {"id": "byName", "options": value_label},
                             "properties": [
                                 {"id": "custom.cellOptions",
                                  "value": {"type": "gauge", "mode": "gradient",
                                            "valueDisplayMode": "text"}},
                                 {"id": "min", "value": 0},
                                 {"id": "fieldMinMax", "value": True},
                                 {"id": "color",
                                  "value": {"mode": "continuous-BlPu"}}]}]
                        if gauge else []},
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



def table_warn_summary():
    """Warning 汇总表：两条查询（次数 / 对象数）按 reason+kind+namespace 合并。

    「对象数」用嵌套聚合算：先按 name 分一层，再 count 掉它，
    得到「这个组合涉及多少个不同对象」。

    次数列的 gauge **必须加 fieldMinMax**：Grafana 默认按整个 frame 的全局
    min/max 刻度，次数(231) 和对象数(32) 量级差得远时条会失真（见坑位 64）。
    """
    idx = {"Time": 0, "reason": 1, "kind": 2, "namespace": 3,
           "Value #A": 4, "Value #B": 5}
    rn = {"reason": "原因", "kind": "类型", "namespace": "命名空间",
          "Value #A": "次数", "Value #B": "对象数"}
    ov = [
        {"matcher": {"id": "byName", "options": "次数"},
         "properties": [
             {"id": "custom.cellOptions",
              "value": {"type": "gauge", "mode": "gradient",
                        "valueDisplayMode": "text"}},
             {"id": "min", "value": 0},
             {"id": "fieldMinMax", "value": True},
             # 次数列**不设固定宽度**：让它吸收剩余空间，gauge 条越长越好读。
             # 其余几列都定宽，否则某一列会吃掉所有剩余宽度。
             {"id": "color", "value": {"mode": "continuous-BlPu"}}]},
        {"matcher": {"id": "byName", "options": "对象数"},
         "properties": [{"id": "custom.width", "value": 110},
                        {"id": "noValue", "value": "—"}]},
        # 原因列不设宽的话会吃掉所有剩余宽度（实测占了约一半面板）
        {"matcher": {"id": "byName", "options": "原因"},
         "properties": [{"id": "custom.width", "value": 260}]},
        {"matcher": {"id": "byName", "options": "类型"},
         "properties": [{"id": "custom.width", "value": 110}]},
        {"matcher": {"id": "byName", "options": "命名空间"},
         "properties": [{"id": "custom.width", "value": 200}]},
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
                                         "sort": [{"field": "次数", "desc": True}]}},
        ],
    }


def tgt(expr, legend=None, ref="A", qtype="range"):
    t = {"refId": ref, "datasource": ds, "expr": expr, "queryType": qtype}
    if legend:
        t["legendFormat"] = legend
    return t


def panel(pid, title, ptype, gp, targets, desc="", extra=None):
    p = {"id": pid, "title": title, "type": ptype, "datasource": ds,
         "gridPos": gp, "targets": targets, "description": desc}
    if extra:
        p.update(extra)
    return p


def row(pid, title, y, collapsed=False, children=None):
    """折叠行。

    **子面板必须嵌在 row["panels"] 里，不能留在顶层 panels 列表。**
    留在顶层的话，行显示为折叠、面板却照样渲染，等于折叠没生效。
    也因此布局重叠检查必须递归，顶层扫一遍是查不到折叠行内部的。
    """
    return {"id": pid, "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "panels": list(children or [])}


TS = {"fieldConfig": {"defaults": {
        "custom": {"lineWidth": 1, "fillOpacity": 15, "showPoints": "never"},
        "unit": "short"}, "overrides": []},
      "options": {"legend": {"displayMode": "list", "placement": "bottom",
                             "showLegend": True},
                  "tooltip": {"mode": "multi", "sort": "desc"}}}


def stat(dec=0, unit="short", steps=None, novalue="0"):
    """noValue：查询无结果时显示什么。

    默认显示 "0" 而不是 Grafana 的 "No data"——这两者语义完全不同：
    「这段时间没有 Warning」是好事，「采集断了」是故障，
    而 "No data" 把两者混成了一个样子。稀疏环境 + 30m 默认范围下，
    第一屏经常全空，没有这个区分就会被当成看板坏了。
    """
    fc = {"unit": unit, "decimals": dec, "noValue": novalue}
    if steps:
        fc["color"] = {"mode": "thresholds"}
        fc["thresholds"] = {"mode": "absolute", "steps": steps}
    return {"fieldConfig": {"defaults": fc, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                        "colorMode": "value" if steps else "none",
                        "graphMode": "area"}}


# 事件是 logfmt 不是 JSON，prettifyLogMessage（JSON 美化）对它没用
LOGS = {"options": {"showTime": True, "showLabels": False,
                    "wrapLogMessage": True, "sortOrder": "Descending",
                    "enableLogDetails": True, "dedupStrategy": "none"}}

# 柱状堆叠时序：事件稀疏，画成折线几乎全是贴地的平线，
# 柱状才能看出「什么时候有、有多少」。
BARS = {"fieldConfig": {"defaults": {
            "custom": {"drawStyle": "bars", "fillOpacity": 80, "lineWidth": 0,
                       "stacking": {"mode": "normal", "group": "A"}},
            "unit": "short"}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom",
                               "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}}}


def stat_text():
    """stat 面板显示**序列名**而不是数值（textMode: name）。
    用来把 topk(1) 查出来的 reason 名字直接摆在第一屏。

    **配套的查询必须是 range，不能是 instant。** 实测 instant 查询下
    stat 不认 field.config.displayNameFromDS（那个值是对的，probe 过），
    显示成 "Value #A"；换成 range 才正常显示 "Unhealthy"。
    """
    return {"fieldConfig": {"defaults": {"unit": "short", "noValue": "—"},
                            "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"],
                                          "fields": "", "values": False},
                        "textMode": "name", "colorMode": "none",
                        "graphMode": "none", "justifyMode": "center"}}


# 说明文本。Grafana 官方建议每个看板放一个，交代用途和怎么读。
# 放在默认折叠的行里：排障时不占第一屏，要看时展开。
DOC = """\
### 这个看板回答什么

**集群里发生了什么异常，涉及哪些对象。**

Kubernetes 事件不在容器日志里——Pod 根本没起来的时候没有容器日志，只有事件。
而且 apiserver 没设 `--event-ttl`，事件在 etcd 里**只留 1 小时**，
过期就永久查不到。收进 Loki 才能事后追溯。

### 怎么读

顶部第一屏**全部是 Warning**（事件的 `type=Warning`，采集时映射成 `level=warn`）。

排障顺序：先看「Warning 事件汇总」——哪一类原因、什么对象类型、哪个命名空间；
再到下面的「Warning 事件流」看原文和具体是哪个对象。

Normal 事件（Scheduled / Pulled / Created 这些例行动作）量大但多数时候是噪声，
收在「全部事件」折叠行里。

### 两个容易误解的地方

- **默认时间范围 30m。** 这是按真实集群的事件量定的——生产环境事件多，
  默认扫 24h 会把查询压垮。本测试环境事件稀疏（24 小时才三百条），
  30m 打开**经常是空的，那不是故障**，往上调时间范围即可。
- **第一屏的 Warning 面板不受顶部「级别」下拉影响**（硬编码 `level="warn"`），
  否则选了 info 整个排障区会全空。级别下拉对「全部事件」和「维度分析」生效。

采集端配置见 README 的「步骤 4b — Alloy Events」。
"""


panels = [
    # ================ 第一屏：全部是 Warning ================
    panel(1, "Warning 事件数", "stat", {"h": 4, "w": 4, "x": 0, "y": 0},
          [tgt('sum(count_over_time(%s [%s]))' % (WARN, RANGE_W),
               "warning", qtype="instant")],
          "仪表盘时间范围内 type=Warning 的事件条数。不受顶部「级别」下拉影响。"
          "**刻意不做阈值着色**：这个数随时间范围线性变化（30m 和 24h 差两个"
          "数量级），任何固定阈值在用户改范围时都会失真，要么天天飘红要么永远不亮。"
          "要「超过多少就告警」用 ruler 规则（带固定时间窗），别用面板颜色，"
          "见坑位 19。空结果显示 0 而不是 No data——「没有 Warning」和"
          "「采集断了」必须能区分开。",
          stat()),

    panel(2, "受影响对象", "stat", {"h": 4, "w": 4, "x": 4, "y": 0},
          [tgt('count(sum by (name) (count_over_time(%s [%s])))' % (WARN, RANGE_W),
               "对象数", qtype="instant")],
          "有 Warning 事件的对象个数，按 name 去重。"
          "和左边一起看：事件数高但对象数只有一两个，通常是单点反复抖动，"
          "不是面上的问题。",
          stat()),

    panel(3, "最频繁故障原因", "stat", {"h": 4, "w": 4, "x": 8, "y": 0},
          [tgt('topk(1, sum by (reason) (count_over_time(%s [%s])))' % (WARN, RANGE_W),
               "{{reason}}")],
          "Warning 里出现次数最多的 reason，直接显示名字而不是数值。"
          "完整分布看下面的汇总表。"
          "这里用 range 查询不是 instant——instant 下 stat 显示成 Value #A，见 stat_text()。",
          stat_text()),

    # 分桶用 $__interval（= Grafana 的 step）而不是速率窗口：后者是滑动窗口，
    # 24h 范围配 5m 窗口画出来是几根几乎看不见的细线。配面板级 interval
    # 设最小步长，避免 step 太细变成密集栅栏。和另外三个看板统一。
    panel(4, "事件趋势（Normal / Warning）", "timeseries",
          {"h": 4, "w": 12, "x": 12, "y": 0},
          [tgt('sum by (level) (count_over_time(%s [$__interval]))' % TREND,
               "{{level}}")],
          "按级别堆叠的事件条数直方图，分桶宽度自动跟随视图。"
          "不受顶部「级别」下拉影响——它本身就是按级别分组的，再被收窄就只剩一种颜色。",
          dict(BARS, interval="5m")),

    # h=10 不是 h=6。实测 h=6 只显示 4 行，而查询返回 7 行——
    # FailedScheduling 和 BackOff 正好被截在视野外，而那恰恰是这个看板
    # 存在的理由。表格高度要按**查询可能返回的行数**定，不是按当下的行数。
    panel(5, "Warning 事件汇总", "table", {"h": 10, "w": 24, "x": 0, "y": 4},
          [tgt('topk(%d, sum by (reason, kind, namespace) (count_over_time(%s [%s])))'
               % (LIST_N, WARN, RANGE_W), None, qtype="instant"),
           tgt('count by (reason, kind, namespace) '
               '(sum by (reason, kind, namespace, name) (count_over_time(%s [%s])))'
               % (WARN, RANGE_W), None, ref="B", qtype="instant")],
          "排障从这里开始：哪一类原因、什么对象类型、哪个命名空间，各多少次、"
          "涉及多少个对象。"
          "**「对象数」补的是影响面**：231 次 / 32 个对象是面上的问题，"
          "8 次 / 1 个对象是单点抖动，光看次数分不出来。"
          "按 reason+kind+namespace 聚合而**不带对象名**——带上会让一次探针抖动"
          "炸成几十行；具体是哪个对象去下面的事件流看。最多 30 行。"
          "这张表取代了原先四个写死 reason 的「常见故障速查」面板，"
          "新出现的故障类型会自动进来，不用改脚本。",
          table_warn_summary()),

    panel(6, "Warning 事件流", "logs", {"h": 10, "w": 24, "x": 0, "y": 14},
          [tgt("%s %s" % (WARN, LINE_FMT))],
          "Warning 事件原文，已用 line_format 重排成「原因 类型/对象 [命名空间] 说明」。"
          "原始行里 objectAPIversion / objectRV / eventRV / reportinginstance "
          "这些字段对人没用却占掉一半行宽，把真正要看的挤到了行尾。"
          "点单条左侧箭头展开，logfmt 解析出的字段"
          "（reason / kind / name / reportingcontroller / msg）都在详情里，"
          "可直接点击做过滤。",
          LOGS),

    # ================ 折叠：全部事件 ================
    row(200, "全部事件（含 Normal）", 24, collapsed=True, children=[
        panel(10, "全部事件", "logs", {"h": 14, "w": 24, "x": 0, "y": 24},
              [tgt("%s %s" % (SEL, LINE_FMT))],
              "包含 Normal。受顶部全部过滤器约束，含「级别」下拉。"
              "Normal 占九成左右（Scheduled / Pulled / Created 等例行动作）。",
              LOGS),
    ]),

    # ================ 折叠：维度分析 ================
    row(300, "维度分析", 25, collapsed=True, children=[
        panel(20, "涉及对象数", "stat", {"h": 8, "w": 6, "x": 0, "y": 25},
              [tgt('count(sum by (name) (count_over_time(%s [%s])))' % (SEL, RANGE_W),
                   "对象数", qtype="instant")],
              "全部事件（含 Normal）涉及的对象个数，受顶部全部过滤器约束。",
              stat()),

        panel(21, "涉及对象列表", "table", {"h": 8, "w": 18, "x": 6, "y": 25},
              [tgt('topk(%d, sum by (name, kind, namespace) (count_over_time(%s [%s])))'
                   % (LIST_N, SEL, RANGE_W), None, qtype="instant")],
              "产生过事件的对象，按事件条数倒序，最多 30 行。"
              "对象数超过 30 时列表会少于左边的计数，是预期行为不是故障。",
              list_table("事件数", {"name": "对象", "kind": "类型",
                                    "namespace": "命名空间"},
                         ["name", "kind", "namespace"], gauge=True)),

        panel(22, "按原因的事件速率", "timeseries", {"h": 8, "w": 12, "x": 0, "y": 33},
              [tgt('sum by (reason) (rate(%s [%s]))' % (SEL, W), "{{reason}}")],
              "突然冒出的新 reason 往往就是故障的起点。", TS),

        panel(23, "按命名空间的事件速率", "timeseries",
              {"h": 8, "w": 12, "x": 12, "y": 33},
              [tgt('sum by (namespace) (rate(%s [%s]))' % (SEL, W), "{{namespace}}")],
              "namespace=_cluster 表示集群级对象（Node/PV 等）的事件。", TS),
    ]),
    # ---- 说明行放在**最后**。
    # Grafana 的 row 是分隔符：排在某个 row 之后的顶层面板会被算作属于该 row。
    # 这个行放最前面的话，后面六个第一屏面板会被当成它的内容一起折叠掉
    # （实测：整屏截图里只剩三个行标题，六个面板全不显示）。
    row(90, "说明 / 怎么读这个看板", 26, collapsed=True, children=[
        {"id": 91, "type": "text", "title": "", "transparent": True,
         "gridPos": {"h": 10, "w": 24, "x": 0, "y": 25},
         "options": {"mode": "markdown", "content": DOC}},
    ]),
]

# 速率窗口档位。默认 5m 要和 30m 的默认时间范围配套——
# 速率窗口比整个时间范围还大的话，分桶就只剩一两个点，趋势图没有意义。
WINDOWS = ["1m", "5m", "15m", "1h", "6h"]

dashboard = {
    "uid": "loki-k8s-events",
    "title": "Kubernetes 事件",
    "description": "集群事件（OOMKilled / 调度失败 / 镜像拉取失败 / 探针失败等）。"
                   "事件不在容器日志里，且 etcd 中只保留 1 小时，"
                   "收进 Loki 才能事后追溯。采集端见 README 步骤 4b。",
    "tags": ["loki", "logs", "kubernetes", "events"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 0,
    "refresh": "1m",
    # 默认 30m：按真实集群的事件量定，不按本测试环境的稀疏程度定。
    # 稀疏环境下打开是空的属于预期，往上调即可；反过来默认 24h 在生产会压垮查询。
    "time": {"from": "now-30m", "to": "now"},
    "editable": True,
    "templating": {"list": [
        # ---- 项目 / 环境：级联链首 ----
        # project 不受任何过滤；environment 只按 project 收窄。
        lv("project", "项目"),

        lv("environment", "环境", '{project=~"$project"}'),

        lv("namespace", "命名空间", '{%s, source="kubernetes-events"}' % PE,
           "_cluster 表示集群级对象（Node/PV 等）的事件"),

        lv("level", "级别",
           '{%s, source="kubernetes-events", namespace=~"$namespace"}' % PE,
           "warn = 事件的 type=Warning，info = Normal"),

        tb("reason", "原因", ".+",
           "按 reason 过滤，正则。例：FailedScheduling|Unhealthy。"
           "reason 是 logfmt 字段不是标签，所以只能手填。默认 .+ 匹配全部"),

        tb("kind", "对象类型", ".+",
           "按涉及对象的 kind 过滤，正则。例：Pod|Node|Deployment。默认 .+ 匹配全部"),

        tb("search", "关键字", "",
           "在事件正文里搜索，留空则不过滤"),

        {"name": "rate_window", "label": "速率窗口", "type": "interval",
         "query": ",".join(WINDOWS), "auto": False, "refresh": 0,
         "current": {"selected": True, "text": "5m", "value": "5m"},
         "options": [{"selected": w == "5m", "text": w, "value": w} for w in WINDOWS]},
    ]},
    "panels": panels,
}

out = "/root/loki-stack/grafana/dashboard-k8s-events.json"
open(out, "w").write(json.dumps(dashboard, ensure_ascii=False, indent=2))
print("已写入:", out)

# folderUid 必须显式给：不给的话 Grafana 会把看板放回 General
# （实测 payload 不带这个字段，返回的 folderUid 是空串），
# 文件夹级的权限配置就随之失效。看板归属属于部署配置，
# 和看板内容一样应当由脚本持有，不靠手工拖拽维持。
payload = {"dashboard": dashboard, "folderUid": "ops-only",
           "overwrite": True,
           "message": "Kubernetes 事件看板"}
r = subprocess.run(
    ["curl", "-s", "--max-time", "30", "-u", AUTH,
     "-H", "Content-Type: application/json",
     "-X", "POST", GRAFANA + "/api/dashboards/db", "-d", json.dumps(payload)],
    capture_output=True, text=True)
print("导入响应:", r.stdout[:250])
