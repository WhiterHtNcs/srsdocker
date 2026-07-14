#!/bin/sh

# --- 配置区 ---
USER_PASS="admin:strive12138" 
# 飞牛暴露的目录已是容器内 /app/mapping 对应的数据目录，无需再加 mapping/。
SRS_URL="http://192.168.5.224:5005/DockerData/srsdocker/rule-set/srs"
OPENCLASH_URL="http://192.168.5.224:5005/DockerData/srsdocker/rule-set/openclash/openclash.yaml"

LOCAL_SRS_DIR="/usr/share/sing-box"
LOCAL_OPENCLASH_DIR="/etc/openclash/config"
LOCAL_OPENCLASH_FILE="$LOCAL_OPENCLASH_DIR/openclash.yaml"

LIST_FILE="$(mktemp /tmp/srs_files.XXXXXX)" || exit 1
OPENCLASH_TMP="$(mktemp /tmp/openclash_config.XXXXXX)" || exit 1
trap 'rm -f "$LIST_FILE" "$OPENCLASH_TMP"' EXIT HUP INT TERM

mkdir -p "$LOCAL_SRS_DIR" "$LOCAL_OPENCLASH_DIR"

# 1. 下载 SRS 清单并同步全部规则文件
if curl -fsSL -u "$USER_PASS" "$SRS_URL/files.txt" -o "$LIST_FILE" && [ -s "$LIST_FILE" ]; then
    while IFS= read -r FILE || [ -n "$FILE" ]; do
        FILE="$(printf '%s' "$FILE" | tr -d '\r')"
        [ -z "$FILE" ] && continue

        if curl -fsSL -u "$USER_PASS" "$SRS_URL/$FILE" -o "$LOCAL_SRS_DIR/$FILE"; then
            echo "已同步 SRS: $FILE"
        else
            echo "❌ SRS 下载失败: $FILE"
        fi
    done < "$LIST_FILE"
    echo "✅ 规则文件已全部备齐，等待下次设备重启生效。"
else
    echo "❌ 无法读取 SRS 清单，保持旧规则。"
fi

# 2. 下载完整 OpenClash 配置。仅在完整下载成功后覆盖旧文件。
if curl -fsSL -u "$USER_PASS" "$OPENCLASH_URL" -o "$OPENCLASH_TMP" && [ -s "$OPENCLASH_TMP" ]; then
    mv "$OPENCLASH_TMP" "$LOCAL_OPENCLASH_FILE"
    echo "✅ OpenClash 配置已同步: $LOCAL_OPENCLASH_FILE"
else
    echo "❌ OpenClash 配置下载失败，保留现有配置。"
fi
