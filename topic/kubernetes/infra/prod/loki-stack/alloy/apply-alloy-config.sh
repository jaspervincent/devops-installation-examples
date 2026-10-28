#!/bin/bash
# 更新 alloy-config 并【确认 reload 真的成功】。
#
# 教训一：alloy validate 只校验 HCL 结构，不解析 stage.match 里的 LogQL selector，
#   也不检查 template 语法。validate 通过 ≠ 能加载。reload 失败时 Alloy 会静默
#   回落到上一份配置继续跑，采集看起来一切正常，但改动根本没生效。
#
# 教训二：查日志必须用 --since-time 锚定到【本次 apply 之后】。用 --since=3m
#   这种相对窗口会把上一次失败尝试的报错也捞进来，明明修好了却一直显示失败。
#
# 教训三：不要用 `kubectl logs ds/alloy`，它只挑其中一个 Pod
#   （提示 "Found 5 pods, using pod/alloy-xxxxx"），另外四个失败了看不见。
#   label 要用 instance=alloy，否则会把独立 release 的 alloy-events 也带进来。
set -u
cd /root/loki-stack/alloy

SEL="app.kubernetes.io/instance=alloy"

T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "本次 apply 时刻: $T0"

out=$(kubectl create cm alloy-config -n alloy \
        --from-file=config.alloy=config.alloy \
        --dry-run=client -o yaml | kubectl apply -f - 2>&1)
echo "$out"

if echo "$out" | grep -q "unchanged"; then
  echo
  echo "ConfigMap 内容与集群里的一致，不会触发 reload —— 这不是错误。"
  echo "如果你刚改过 config.alloy 却看到 unchanged，说明改动没写进文件。"
  exit 0
fi

echo "等待 reload（kubelet 传播 ConfigMap 实测 10~77 秒）..."
ok=0
for i in $(seq 1 15); do
  sleep 10
  ok=$(kubectl logs -n alloy -l "$SEL" -c alloy --since-time="$T0" --prefix 2>/dev/null \
       | grep -c "config reloaded")
  [ "$ok" -gt 0 ] && break
done

echo
echo "== apply 之后的 reload 成功次数: $ok =="
if [ "$ok" -eq 0 ]; then
  echo ">>> 超时未见 reload，检查 config-reloader 边车 <<<"
  exit 1
fi

echo "== apply 之后的加载错误（必须为空）=="
err=$(kubectl logs -n alloy -l "$SEL" -c alloy --since-time="$T0" --prefix 2>/dev/null \
      | grep -E "failed to reload config|failed to evaluate config|invalid stage config" | tail -3)
if [ -n "$err" ]; then
  echo "$err"
  echo
  echo ">>> reload 失败，Alloy 仍在用旧配置 <<<"
  exit 1
fi
echo "  （无）"

echo
echo "== 各实例 apply 之后的最后一条 reload 状态 =="
for p in $(kubectl get pods -n alloy -l "$SEL" -o name); do
  # 注意用变量接结果再判空：`grep | tail` 的退出码来自 tail（恒为 0），
  # 写成 `grep ... || echo 无` 不会生效
  s=$(kubectl logs -n alloy "$p" -c alloy --since-time="$T0" 2>/dev/null \
      | grep -oE "config reloaded|failed to reload config" | tail -1)
  printf "  %-34s %s\n" "${p#pod/}" "${s:-（本次无记录）}"
done
