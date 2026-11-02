#!/usr/bin/env bash
#
# 按本机设备树生成/修正 camd.conf 的相机映射
# ================================================
# camd.conf 里 cam=<角色>,<sensor名> 的 sensor 名（mXX_b_xxx）由设备树命名，
# 不同 DTB 版本可能不一致（如 m00/m02/m03/m04 vs m00~m03 连续编号），
# I2C 总线编号也随内核 probe 顺序变化；只有控制器 MMIO 地址（PCB 走线）恒定。
# 本脚本按 MMIO → 角色的固定映射，从本机设备树读出实际 sensor 名重写 cam= 行。
#
# 相机角色 ↔ I2C 控制器的接线关系随硬件型号不同（参考 config/v4l2_isp/
# 各型号 YAML 的 role ↔ /dev/videoN 差异），需按 HARDWARE_MODEL 选映射表。
#
# 用法: gen_camd_conf.sh [camd.conf 路径] [硬件型号]
#   硬件型号缺省时依次尝试: $EGORECORD_HOME/HARDWARE_MODEL >
#   /opt/egorecord/HARDWARE_MODEL > ~/egorecord/HARDWARE_MODEL，
#   均无则按默认型号 baai-wuyu-ego4-01-171 处理。
# 任何一路解析失败则保留原配置并返回非 0（camd 启动报错时会指出缺失的 sensor 名）。
# 由 install_release.sh / install_autostart.sh 在安装 camd 时自动调用，也可手动运行。
#
set -euo pipefail

CONF="${1:-$(cd "$(dirname "$0")" && pwd)/camd.conf}"
[[ -f "$CONF" ]] || { echo "camd 配置不存在: $CONF" >&2; exit 1; }

DTBASE="/sys/firmware/devicetree/base"
[[ -d "$DTBASE" ]] || { echo "无法访问设备树 $DTBASE" >&2; exit 1; }

# ── 硬件型号 → 角色/MMIO 映射表 ──────────────────────────────
# MMIO 为 I2C 控制器地址（PCB 走线，不随 DTB/内核版本变化）。
# 表 A: baai-wuyu-ego4-01-155 / 01-171（对应 v4l2_isp_ego4_01_15x.yaml：
#        left=video71  right=video62  bright=video44  bleft=video53）
# 表 B: baai-wuyu-ego4-0150-152 / 0151-152（v4l2_isp_ego4_015x_152.yaml：
#        left=video44  right=video53  bright=video71  bleft=video62）
# videoN ↔ 控制器绑定（两种版型一致）：video44=feca0000 video53=fea90000
#                                      video71=feac0000 video62=feab0000
HW_MODEL="${2:-}"
if [[ -z "$HW_MODEL" ]]; then
    for f in ${EGORECORD_HOME:+"$EGORECORD_HOME/HARDWARE_MODEL"} \
             /opt/egorecord/HARDWARE_MODEL "$HOME/egorecord/HARDWARE_MODEL"; do
        if [[ -f "$f" ]]; then
            HW_MODEL="$(cat "$f")"
            break
        fi
    done
fi
HW_MODEL="${HW_MODEL:-baai-wuyu-ego4-01-171}"

case "$HW_MODEL" in
    baai-wuyu-ego4-0150-152|baai-wuyu-ego4-0151-152)
        MAP_TABLE="B"
        ROLE_MMIO_LEFT=feca0000;  ROLE_MMIO_RIGHT=fea90000
        ROLE_MMIO_BRIGHT=feac0000; ROLE_MMIO_BLEFT=feab0000
        ;;
    baai-wuyu-ego4-01-155|baai-wuyu-ego4-01-171)
        MAP_TABLE="A"
        ROLE_MMIO_LEFT=feac0000;  ROLE_MMIO_RIGHT=feab0000
        ROLE_MMIO_BRIGHT=feca0000; ROLE_MMIO_BLEFT=fea90000
        ;;
    *)
        echo "⚠ 未知硬件型号 \"$HW_MODEL\"，按默认表 A (01-155/01-171) 处理" >&2
        MAP_TABLE="A"
        ROLE_MMIO_LEFT=feac0000;  ROLE_MMIO_RIGHT=feab0000
        ROLE_MMIO_BRIGHT=feca0000; ROLE_MMIO_BLEFT=fea90000
        ;;
esac

declare -A ROLE_MMIO=(
    [left]="$ROLE_MMIO_LEFT"
    [right]="$ROLE_MMIO_RIGHT"
    [bright]="$ROLE_MMIO_BRIGHT"
    [bleft]="$ROLE_MMIO_BLEFT"
)

declare -A ROLE_SNS=()
for role in left right bright bleft; do
    ctrl="${ROLE_MMIO[$role]}"
    node=""
    for d in "$DTBASE"/i2c@"$ctrl"/*/; do
        [[ -f "${d}rockchip,camera-module-index" ]] && node="$d" && break
    done
    if [[ -z "$node" ]]; then
        echo "⚠ i2c@$ctrl 下未找到相机 sensor 节点，保留原配置" >&2
        exit 1
    fi
    # module-index 为大端 u32，取末字节（索引 < 256）；facing 取首字符（back→b）
    idx=$(od -An -tu1 "${node}rockchip,camera-module-index" 2>/dev/null | awk '{print $NF}')
    facing=$(tr -d '\0' < "${node}rockchip,camera-module-facing" 2>/dev/null)
    sns=$(basename "$node"); sns="${sns%@*}"; sns="${sns%%-*}"
    if [[ -z "$idx" || -z "$facing" || -z "$sns" ]]; then
        echo "⚠ i2c@$ctrl sensor 节点属性不完整，保留原配置" >&2
        exit 1
    fi
    ROLE_SNS[$role]=$(printf "m%02d_%s_%s" "$idx" "${facing:0:1}" "$sns")
done

# 软校验：sensor 名应出现在内核日志的 dphy 匹配记录里（dmesg 环形缓冲区
# 可能已覆盖，查不到只警告不失败）
DMESG_TXT=$(dmesg 2>/dev/null || true)
for role in left right bright bleft; do
    if [[ -n "$DMESG_TXT" ]] && ! grep -q "${ROLE_SNS[$role]}" <<< "$DMESG_TXT"; then
        echo "⚠ dmesg 中未找到 ${ROLE_SNS[$role]}，请确认相机已就绪" >&2
    fi
    sed -i -E "s|^cam=${role},.*|cam=${role},${ROLE_SNS[$role]}|" "$CONF"
done

echo "✓ camd 相机映射已生成 ($CONF)"
echo "    硬件型号: $HW_MODEL (映射表 $MAP_TABLE)"
grep "^cam=" "$CONF" | sed 's/^/    /'
