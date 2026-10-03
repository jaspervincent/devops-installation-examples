#!/bin/bash
# 给 Gateway 造【每个接口画像不同】的流量，用来验证接口表的三列视觉编码。
#
# 和 gen-gateway-traffic.sh 的区别：那个脚本所有接口的错误率和延迟都一样，
# 表格里三列数值高度雷同，根本验证不出颜色阈值和 gauge 有没有效果。
# 这里给每个接口一个固定画像（方法 / 权重 / 错误率 / 延迟），
# 跑完之后表格应当出现明显的绿-橙-红分布。
#
# 画像：接口|方法|权重|错误率%|延迟ms（0 表示不加 echo_time）
#   /health        GET    高   0%   极快   -> 全绿
#   /api/products  GET    高   2%   300ms  -> 绿
#   /api/users     GET    高   3%   0      -> 绿
#   /static/app.js GET    中   0%   0      -> 绿
#   /api/orders    POST   高   8%   0      -> 错误率橙
#   /login         POST   中  18%   600ms  -> 错误率红 + P95 橙
#   /api/checkout  POST   中  35%  1500ms  -> 两列都红
#   /admin/export  DELETE 低  60%  2500ms  -> 极端行，验证阈值上限
set -u
GW=http://10.0.0.14
HOST=echo.jasper.org
DUR=${1:-240}          # 总时长秒，默认 4 分钟

# 接口画像表：path method weight errpct delayms
PROFILES=(
  "/health GET 6 0 0"
  "/api/products GET 5 2 300"
  "/api/users GET 5 3 0"
  "/static/app.js GET 4 0 0"
  "/api/orders POST 5 8 0"
  "/login POST 3 18 600"
  "/api/checkout POST 3 35 1500"
  "/admin/export DELETE 1 60 2500"
)

# 按权重展开成抽样池
POOL=()
for p in "${PROFILES[@]}"; do
  set -- $p
  for _ in $(seq 1 "$3"); do POOL+=("$1 $2 $4 $5"); done
done

one() {
  set -- ${POOL[$((RANDOM % ${#POOL[@]}))]}
  local path=$1 method=$2 errpct=$3 delay=$4 q=""
  # 错误码：按该接口的错误率投骰子，命中则在 500/503/404 里挑一个
  if [ $((RANDOM % 100)) -lt "$errpct" ]; then
    case $((RANDOM % 3)) in
      0) q="?echo_code=500" ;;
      1) q="?echo_code=503" ;;
      2) q="?echo_code=404" ;;
    esac
  elif [ "$delay" -gt 0 ]; then
    # 延迟有抖动，否则 P95 和中位数一样，看不出分位数的意义
    q="?echo_time=$((delay / 2 + RANDOM % delay))"
  fi
  curl -s -o /dev/null --max-time 15 -X "$method" -H "Host: $HOST" "$GW$path$q"
}

echo "造流量 ${DUR}s，8 个接口各自画像"
end=$(( $(date +%s) + DUR ))
while [ "$(date +%s)" -lt "$end" ]; do
  for _ in $(seq 1 8); do one & done
  sleep 1
done
wait
echo "[$(date +%H:%M:%S)] 完成，残留 curl 进程数: $(pgrep -c -f 'curl.*echo[.]jasper[.]org' || echo 0)"
