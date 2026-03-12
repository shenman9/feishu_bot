#!/usr/bin/env bash
# 批量回溯论文日报：从 3 月 1 日到 3 月 9 日
# 用法: bash backfill_range.sh

set -e

START_DATE="20260301"
END_DATE="20260309"

current="$START_DATE"
while [ "$current" -le "$END_DATE" ]; do
    echo ""
    echo "========== 回溯日期: $current =========="
    python -m plugins.paper_daily.backfill "$current"

    # 每天之间暂停 5 秒，避免 API 限流
    if [ "$current" != "$END_DATE" ]; then
        echo "等待 5 秒..."
        sleep 5
    fi

    # 日期 +1 天
    current=$(date -d "${current:0:4}-${current:4:2}-${current:6:2} +1 day" +%Y%m%d)
done

echo ""
echo "========== 全部回溯完成 =========="
