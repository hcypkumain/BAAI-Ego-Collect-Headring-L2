#!/bin/bash
# IQ 配置管理脚本: 部署 / 回退 / 状态查看
# 用法: sudo bash switch_iq.sh [deploy|restore|status]
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IQ_NEW="$SCRIPT_DIR/sc233hgs_default_default.json"
IQ_ROS="/opt/ros/noetic/share/seeker_cam_bridge/launch/sc233hgs_default_default.json"
IQ_ETC="/etc/iqfiles/sc233hgs_default_default.json"
BAK_ROS="${IQ_ROS}.bak_original"
BAK_ETC="${IQ_ETC}.bak"

# ---------- helpers ----------

_check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "❌ 请使用 sudo 运行: sudo bash $0 $1"
        exit 1
    fi
}

_backup_if_needed() {
    if [ -f "$IQ_ROS" ] && [ ! -f "$BAK_ROS" ]; then
        cp "$IQ_ROS" "$BAK_ROS"
        echo "📦 备份 ROS IQ → $BAK_ROS"
    fi
    if [ -f "$IQ_ETC" ] && [ ! -f "$BAK_ETC" ]; then
        cp "$IQ_ETC" "$BAK_ETC"
        echo "📦 备份 /etc IQ → $BAK_ETC"
    fi
}

_deploy_file() {
    local src="$1"
    # ROS 桥路径仅在其存在时同步（camd 部署可能无 ROS）
    if [ -d "$(dirname "$IQ_ROS")" ]; then
        cp "$src" "$IQ_ROS"
    fi
    cp "$src" "$IQ_ETC"
    # IQ 由 rkaiq 初始化时加载，必须重启当前在用的采集服务才生效
    if systemctl is-active --quiet camd; then
        systemctl restart camd
        sleep 2
        if systemctl is-active --quiet camd; then
            echo "✅ camd 已重启（新 IQ 生效）"
        else
            echo "❌ camd 启动失败"
            systemctl status camd --no-pager
            exit 1
        fi
    elif systemctl is-active --quiet seeker_cam_bridge; then
        systemctl restart seeker_cam_bridge
        sleep 2
        if systemctl is-active --quiet seeker_cam_bridge; then
            echo "✅ seeker_cam_bridge 运行中（新 IQ 生效）"
        else
            echo "❌ seeker_cam_bridge 启动失败"
            systemctl status seeker_cam_bridge --no-pager
            exit 1
        fi
    else
        echo "⚠️ camd / seeker_cam_bridge 均未运行，IQ 文件已部署，下次启动采集服务时生效"
    fi
}

# ---------- commands ----------

cmd_deploy() {
    _check_root deploy
    if [ ! -f "$IQ_NEW" ]; then
        echo "❌ IQ 文件不存在: $IQ_NEW"; exit 1
    fi
    _backup_if_needed
    echo "🔄 部署新 IQ 配置..."
    _deploy_file "$IQ_NEW"
    echo ""
    echo "✅ 已切换到 【调优方案】(快门优先, 最慢1/50s, 增益≤2×, HIGHLIGHT)"
    echo "   快门路线: 最快~1/50s | 增益上限: 2× | HIGHLIGHT 防过曝 | 1200全画幅"
    echo "   回退: sudo bash $0 restore"
}

cmd_restore() {
    _check_root restore
    if [ -f "$BAK_ROS" ]; then
        echo "🔄 回退到原始 IQ 配置..."
        _deploy_file "$BAK_ROS"
        echo "✅ 已切换到 【原始方案】(1/50s, 无HDR)"
    elif [ -f "$BAK_ETC" ]; then
        echo "🔄 从 /etc 备份回退..."
        _deploy_file "$BAK_ETC"
        echo "✅ 已切换到 【原始方案】"
    else
        echo "❌ 找不到备份文件，无法回退"; exit 1
    fi
}

cmd_status() {
    echo "=== IQ 配置状态 ==="
    echo ""
    # camd 部署可能无 ROS 路径，/etc/iqfiles 才是 rkaiq 实际加载的位置
    local CUR_FILE=""
    [ -f "$IQ_ETC" ] && CUR_FILE="$IQ_ETC"
    [ -z "$CUR_FILE" ] && [ -f "$IQ_ROS" ] && CUR_FILE="$IQ_ROS"
    if [ -n "$CUR_FILE" ]; then
        CUR_MD5=$(md5sum "$CUR_FILE" | awk '{print $1}')
        local BAK_FILE=""
        [ -f "$BAK_ETC" ] && BAK_FILE="$BAK_ETC"
        [ -z "$BAK_FILE" ] && [ -f "$BAK_ROS" ] && BAK_FILE="$BAK_ROS"
        if [ -n "$BAK_FILE" ]; then
            BAK_MD5=$(md5sum "$BAK_FILE" | awk '{print $1}')
            if [ "$CUR_MD5" = "$BAK_MD5" ]; then
                echo "  版本: 📌 出厂原始方案 (增益≤4×, LOWLIGHT)"
            else
                echo "  版本: 📌 调优方案 (最慢1/50s, 增益≤2×, HIGHLIGHT, 1200全画幅)"
            fi
        else
            echo "  版本: 📌 未知 (无备份对比)"
        fi
        echo "  文件: $CUR_FILE"
        echo "  MD5:  $CUR_MD5"
    else
        echo "  ❌ IQ 文件不存在"
    fi
    echo ""
    echo "  备份:"
    [ -f "$BAK_ROS" ] && echo "    ✅ $BAK_ROS" || echo "    ❌ 无 ROS 备份"
    [ -f "$BAK_ETC" ] && echo "    ✅ $BAK_ETC"  || echo "    ❌ 无 /etc 备份"
    echo ""
    echo "  采集服务（IQ 由 rkaiq 初始化时加载，换 IQ 需重启在用的服务）:"
    systemctl is-active --quiet camd 2>/dev/null \
        && echo "    ✅ camd 运行中" || echo "    ❌ camd 未运行"
    systemctl is-active --quiet seeker_cam_bridge 2>/dev/null \
        && echo "    ✅ seeker_cam_bridge 运行中" || echo "    ❌ seeker_cam_bridge 未运行"
}

# ---------- main ----------

case "${1:-status}" in
    deploy)  cmd_deploy  ;;
    restore) cmd_restore ;;
    status)  cmd_status  ;;
    *)
        echo "用法: sudo bash $0 [deploy|restore|status]"
        echo "  deploy  → 部署调优 IQ (快门优先, 最慢1/50s, 增益≤2×, HIGHLIGHT, 1200全画幅), 自动备份原始文件"
        echo "  restore → 回退到原始 IQ"
        echo "  status  → 查看当前 IQ 状态"
        ;;
esac
