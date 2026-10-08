#!/usr/bin/env bash
# ============================================================
# 在 Proxmox 宿主机上运行：给 LXC 容器把 swap 加到和 RAM 一样大
#
# 用法：  scp 到宿主，然后  bash proxmox-add-swap.sh
#   或：  bash proxmox-add-swap.sh --target 8192
#
# 做三件事：
#   1. 确认宿主机自己有足够的 swap（没有就建）
#   2. 找到目标容器的 CTID
#   3. 把容器的 swap 限额设成 8192MB（= 8G RAM）
# ============================================================
set -euo pipefail

CT_HOSTNAME="${CT_HOSTNAME:-bigone}"     # 目标容器的主机名
TARGET_SWAP_MB="${TARGET_SWAP_MB:-8192}" # 想给容器多少 swap（MB）
HOST_SWAP_FILE="/swapfile"
HOST_SWAP_SIZE="16G"                     # 宿主机 swapfile 大小（要 >= 容器分到的量）

while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET_SWAP_MB="$2"; shift 2 ;;
    --hostname) CT_HOSTNAME="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行"; exit 1; }
command -v pct >/dev/null || { echo "找不到 pct —— 这个脚本必须在 Proxmox 宿主机上跑，不是容器里"; exit 1; }

echo "=========================================="
echo "目标容器: $CT_HOSTNAME   目标 swap: ${TARGET_SWAP_MB}MB"
echo "=========================================="

# ---------- 1. 宿主机 swap 现状 ----------
echo
echo "【1/3】宿主机 swap 现状"
free -m | awk 'NR==1 || /Swap/'
HOST_SWAP_TOTAL=$(awk '/^SwapTotal:/{print int($2/1024)}' /proc/meminfo)

if [ "$HOST_SWAP_TOTAL" -lt "$TARGET_SWAP_MB" ]; then
  echo
  echo "宿主机 swap 只有 ${HOST_SWAP_TOTAL}MB，不够给容器 ${TARGET_SWAP_MB}MB。"
  echo "→ 先在宿主机建 swapfile（${HOST_SWAP_SIZE}）"

  if swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$HOST_SWAP_FILE"; then
    echo "   $HOST_SWAP_FILE 已经是 swap 了，跳过创建"
  elif [ -e "$HOST_SWAP_FILE" ]; then
    echo "   $HOST_SWAP_FILE 已存在但不是 swap，请手动检查后重跑"
    exit 1
  else
    # ZFS 根上不适合用普通 swapfile（会死锁），检测一下
    if df -T / | awk 'NR==2{print $2}' | grep -qi zfs; then
      echo "   ✗ 检测到根文件系统是 ZFS，不能用普通 swapfile（会死锁）！"
      echo "     请改用 ZFS zvol 方式，命令如下："
      echo
      echo "     zfs create -V ${HOST_SWAP_SIZE} -b \$(getconf PAGESIZE) -o compression=zle \\"
      echo "       -o logbias=throughput -o sync=always \\"
      echo "       -o primarycache=metadata -o secondarycache=none \\"
      echo "       -o com.sun:auto-snapshot=false rpool/swap"
      echo "     mkswap /dev/zvol/rpool/swap"
      echo "     echo '/dev/zvol/rpool/swap none swap defaults 0 0' >> /etc/fstab"
      echo "     swapon /dev/zvol/rpool/swap"
      echo
      echo "     跑完上面四条再重新执行本脚本。"
      exit 1
    fi
    echo "   创建 ${HOST_SWAP_SIZE} swapfile ..."
    fallocate -l "$HOST_SWAP_SIZE" "$HOST_SWAP_FILE" || \
      dd if=/dev/zero of="$HOST_SWAP_FILE" bs=1M count=$(( ${HOST_SWAP_SIZE%G} * 1024 )) status=none
    chmod 600 "$HOST_SWAP_FILE"
    mkswap "$HOST_SWAP_FILE" >/dev/null
    swapon "$HOST_SWAP_FILE"
    grep -q "^${HOST_SWAP_FILE} " /etc/fstab || \
      echo "${HOST_SWAP_FILE} none swap sw 0 0" >> /etc/fstab
    echo "   ✓ 已启用并写入 /etc/fstab"
  fi
  echo
  free -m | awk '/Swap/'
else
  echo "  ✓ 宿主机 swap 足够（${HOST_SWAP_TOTAL}MB）"
fi

# ---------- 2. 找容器 ----------
echo
echo "【2/3】查找容器 $CT_HOSTNAME"
CTID=""
for id in $(pct list | awk 'NR>1{print $1}'); do
  h=$(pct config "$id" 2>/dev/null | sed -n 's/^hostname: *//p')
  if [ "$h" = "$CT_HOSTNAME" ]; then CTID="$id"; break; fi
done

if [ -z "$CTID" ]; then
  echo "  ✗ 没找到 hostname=$CT_HOSTNAME 的容器。现有容器："
  pct list
  exit 1
fi
echo "  ✓ CTID = $CTID"
echo "  当前配置："
pct config "$CTID" | grep -E '^(hostname|memory|swap|cores)' | sed 's/^/    /'

# ---------- 3. 设置容器 swap ----------
echo
echo "【3/3】把容器 swap 设为 ${TARGET_SWAP_MB}MB"
pct set "$CTID" --swap "$TARGET_SWAP_MB"
echo "  ✓ 已写入 /etc/pve/lxc/${CTID}.conf"
pct config "$CTID" | grep -E '^(memory|swap)' | sed 's/^/    /'

echo
echo "  注意：swap 限额在容器重启后生效。执行："
echo "      pct reboot $CTID"
echo
echo "  重启后进容器验证："
echo "      free -m        # Swap 那行应显示 ${TARGET_SWAP_MB}"
echo "      swapon --show"
echo
echo "=========================================="
echo "完成。别忘了 pct reboot $CTID"
echo "=========================================="
