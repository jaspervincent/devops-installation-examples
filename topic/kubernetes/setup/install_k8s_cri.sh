#!/bin/env bash
#

. /etc/os-release

KUBE_VERSION="1.36.1"
CFSS_VERSION=1.6.5

IMAGES_URL="registry.aliyuncs.com/google_containers"

DOMAIN=jasper.org

# instance machine
KUBEAPI_IP=10.103.236.236
MASTER1_IP=10.103.236.201
MASTER2_IP=10.103.236.202
MASTER3_IP=10.103.236.203
NODE1_IP=10.103.236.204
NODE2_IP=10.103.236.205

# /etc/hosts
HOSTS="
$KUBEAPI_IP kubeapi.$DOMAIN kubeapi
$MASTER1_IP masert01.$DOMAIN master01
$MASTER2_IP masert02.$DOMAIN master02
$MASTER3_IP masert03.$DOMAIN master03
$NODE1_IP node01.$DOMAIN node01
$NODE2_IP node02.$DOMAIN node02
"

LOCAL_IP=`hostname -I|awk '{print $1}'`

COLOR_SUCCESS="echo -e \\033[1;32m"
COLOR_FAILURE="echo -e \\033[1;31m"
END="\33[m"

color() {
    RES_COL=80
    MOVE_TO_COL="echo -en \\033[${RES_COL}G"
    SETCOLOR_SUCCESS="echo -en \\033[1;32m"
    SETCOLOR_FAILURE="echo -en \\033[1;31m"
    SETCOLOR_WARNING="echo -en \\033[1;33m"
    SETCOLOR_NORMAL="echo -en \E[0m"
    echo -n "$1" && $MOVE_TO_COL
    echo -n "["
    if [ $2 = "success" -o $2 = "0" ]; then
  ${SETCOLOR_SUCCESS}
  echo -n $"  OK  "
    elif [ $2 = "failure" -o $2 = "1" ]; then
  ${SETCOLOR_FAILURE}
  echo -n $"FAILED"
    else
  ${SETCOLOR_WARNING}
  echo -n $"WARNING"
    fi
    ${SETCOLOR_NORMAL}
    echo -n "]"
    echo
}

check() {
  if [ $ID = 'ubuntu' ] && [[ ${VERSION} =~ 2[024].04 ]]; then
    return
  else
    color "不支持此操作系统，退出！" 1
  fi
}

install_prepare() {
  echo "$HOSTS" |grep -q $LOCAL_IP || { color "当前主机 IP 不存在，检查脚本中的 IP 地址信息！" 1 ; exit 1; }
  grep -q $LOCAL_IP /etc/hosts || echo "$HOSTS" >> /etc/hosts
  HOST_NAME=$(awk -v ip=$LOCAL_IP '{if($1==ip && $2 !~ "kubeapi")print $2}' /etc/hosts)
  hostnamectl set-hostname $HOST_NAME || { color "主机名配置失败，检查 /etc/hosts 文件" 1 ; exit 1; }
  swapoff -a
  sed -i '/swap/s/^/#/' /etc/fstab
  color "安装前准备完成！" 0
  sleep 1
}

main() {
  install_prepare
}

main
