#!/bin/bash
# 给 Gateway 造一段【分阶段变化】的流量，用来验证概览面板
# （QPS / 错误率 / P95 / 慢请求）能不能反映真实变化。
#
# 之前面板显示 0 不是面板坏了：零星几个请求分摊到 5 分钟窗口，
# rate 算出来 0.02 req/s，四舍五入就是 0。要看出变化必须有持续流量。
#
# 分三段是刻意的——恒定流量只能画出一条平线，看不出面板到底有没有在跟随变化。
#
# 状态码与延迟分布（贴近真实访问）：
#   ~82% 正常 200
#   ~5%  慢请求（echo_time 1.2~2.5s，用来喂 P95 和「慢请求 >1s」面板）
#   ~5%  404、~4% 500、~4% 503
set -u
GW=http://10.0.0.14
HOST=echo.jasper.org
PATHS=(/ /api/orders /api/users /api/products /static/app.js /health /api/checkout /login)

one() {
  local r=$((RANDOM % 100))
  local p=${PATHS[$((RANDOM % ${#PATHS[@]}))]}
  local q=""
  if   [ $r -lt 5  ]; then q="?echo_time=$((1200 + RANDOM % 1300))"
  elif [ $r -lt 10 ]; then q="?echo_code=404"
  elif [ $r -lt 14 ]; then q="?echo_code=500"
  elif [ $r -lt 18 ]; then q="?echo_code=503"
  fi
  curl -s -o /dev/null --max-time 10 -H "Host: $HOST" "$GW$p$q"
}

phase() {
  local name=$1 secs=$2 rps=$3
  echo "  [$(date +%H:%M:%S)] $name：${rps} req/s × ${secs}s"
  local end=$(( $(date +%s) + secs ))
  while [ "$(date +%s)" -lt "$end" ]; do
    for _ in $(seq 1 "$rps"); do one & done
    sleep 1
  done
}

echo "开始造流量（约 7 分钟）"
phase "低谷" 120 2
phase "高峰" 180 12
phase "回落" 120 4
wait
echo "  [$(date +%H:%M:%S)] 完成"
