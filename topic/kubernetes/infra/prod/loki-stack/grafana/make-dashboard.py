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


panels = [
    # ---------------- 第 1 行：总览统计 ----------------
    panel(1, "总日志速率", "stat", {"h": 4, "w": 6, "x": 0, "y": 0},
          [target('sum(rate({' + JOB + ', ' + SEL + '}[%s]))' % W, "条/秒")],
          "全部来源（Pod + journal）的日志写入速率",
          stat_opts(1, [{"color": "green", "value": None},
                        {"color": "orange", "value": 300},
                        {"color": "red", "value": 500}])),

    # 固定用 1h 窗口且统计【全部来源】：空闲的 control-plane 节点一小时可能
    # 只产生几条日志，用 $rate_window（默认 5m）且只数 journal 会漏掉节点，
    # 面板会误报红色。
    panel(2, "上报节点数 (1h)", "stat", {"h": 4, "w": 6, "x": 6, "y": 0},
          [target('count(count by (node) (count_over_time({node=~".+", ' + SEL + '}[1h])))', "节点",
                  level=False)],
          "过去 1 小时有日志上报的节点数（Pod + journal 全部来源）。"
          "低于集群节点总数说明有节点的 Alloy 停了或节点失联。",
          stat_opts(0, [{"color": "red", "value": None},
                        {"color": "orange", "value": 4},
                        {"color": "green", "value": 5}], graph="none")),

    panel(3, "错误日志速率", "stat", {"h": 4, "w": 6, "x": 12, "y": 0},
          [target('sum(rate({namespace=~".+", namespace!="loki", ' + SEL + '} '
                  '|~ `(?i)(^|[^a-z])(error|fatal|panic)([^a-z]|$)` [%s]))' % W, "条/秒")],
          "业务命名空间的错误日志速率。排除 loki 命名空间：它的查询日志含大量 error 字样会造成自激噪声",
          stat_opts(2, [{"color": "green", "value": None},
                        {"color": "orange", "value": 1},
                        {"color": "red", "value": 2}])),

    panel(4, "活跃 namespace 数", "stat", {"h": 4, "w": 6, "x": 18, "y": 0},
          [target('count(sum by (namespace) (count_over_time({namespace=~".+", ' + SEL + '}[%s])))' % W, "个")],
          "有日志产出的命名空间数量",
          stat_opts(0, graph="none")),

    # ---------------- 第 2 行：分布趋势 ----------------
    panel(10, "按 namespace 的日志速率", "timeseries", {"h": 8, "w": 12, "x": 0, "y": 4},
          [target('sum by (namespace) (rate({namespace=~".+", ' + SEL + '}[%s]))' % W, "{{namespace}}")],
          "Kubernetes Pod 日志，按命名空间聚合", ts_opts),

    panel(11, "按 node 的日志速率", "timeseries", {"h": 8, "w": 12, "x": 12, "y": 4},
          [target('sum by (node) (rate({node=~".+", ' + SEL + '}[%s]))' % W, "{{node}}")],
          "全部来源，按节点聚合。某条线掉到 0 说明该节点 Alloy 可能异常", ts_opts),

    # ---------------- 第 3 行：系统日志 ----------------
    panel(20, "systemd unit 日志速率 (Top 10)", "timeseries", {"h": 8, "w": 12, "x": 0, "y": 12},
          [target('topk(10, sum by (unit) (rate({job="systemd-journal", ' + SEL + '}[%s])))' % W, "{{unit}}")],
          "宿主机 systemd 服务日志，含 kubelet / containerd 等", ts_opts),

    panel(21, "错误日志速率 (按 namespace)", "timeseries", {"h": 8, "w": 12, "x": 12, "y": 12},
          [target('sum by (namespace) (rate({namespace=~".+", namespace!="loki", ' + SEL + '} '
                  '|~ `(?i)(^|[^a-z])(error|fatal|panic)([^a-z]|$)` [%s]))' % W, "{{namespace}}")],
          "排除 loki 命名空间，避免其查询日志造成自激噪声", ts_opts),

    # ---------------- 第 4 行：Top 榜 ----------------
    panel(30, "日志量最大的 Pod (Top 10)", "timeseries", {"h": 8, "w": 12, "x": 0, "y": 20},
          [target('topk(10, sum by (namespace, pod) (rate({namespace=~".+", ' + SEL + '}[%s])))' % W,
                  "{{namespace}}/{{pod}}")],
          "定位刷日志的 Pod", ts_opts),

    panel(31, "kubelet 错误速率 (按节点)", "timeseries", {"h": 8, "w": 12, "x": 12, "y": 20},
          [target('sum by (node) (rate({unit="kubelet.service", ' + SEL + '} '
                  '|~ `(?i)(^|[^a-z])(error|failed)([^a-z]|$)` [%s]))' % W, "{{node}}")],
          "kubelet 报错是节点级问题的早期信号", ts_opts),

    # ---------------- 第 5 行：实时日志 ----------------
    panel(40, "实时日志流", "logs", {"h": 12, "w": 24, "x": 0, "y": 28},
          [target('{namespace=~"$namespace", node=~"$node", ' + JOB + ', ' + SEL + '}')],
          "受顶部 namespace / node 变量过滤",
          {"options": {"showTime": True, "showLabels": False, "wrapLogMessage": True,
                       "sortOrder": "Descending", "enableLogDetails": True,
                       "dedupStrategy": "none"}}),

    panel(41, "错误日志流", "logs", {"h": 12, "w": 24, "x": 0, "y": 40},
          [target('{namespace=~"$namespace", node=~"$node", ' + JOB + ', ' + SEL + '} '
                  '|~ `(?i)(^|[^a-z])(error|fatal|panic)([^a-z]|$)` '
                  '!~ `caller=(metrics|roundtrip|engine)\\.go`')],
          "只显示含 error / fatal / panic 的行。"
          "末尾的 !~ 用于剔除 Loki 自身 querier/query-frontend/ruler 的查询日志——"
          "它们会把查询语句原样打进日志，其中含 error 字样，否则会淹没真实错误。",
          {"options": {"showTime": True, "showLabels": True, "wrapLogMessage": True,
                       "sortOrder": "Descending", "enableLogDetails": True,
                       "dedupStrategy": "none"}}),
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

payload = {"dashboard": dashboard, "overwrite": True,
           "message": "fix: $__rate_interval 不被 Loki 数据源插值，改用 $rate_window"}

r = subprocess.run(
    ["curl", "-s", "--max-time", "30", "-u", AUTH,
     "-H", "Content-Type: application/json",
     "-X", "POST", GRAFANA + "/api/dashboards/db", "-d", json.dumps(payload)],
    capture_output=True, text=True)
print("导入响应:", r.stdout[:250])
