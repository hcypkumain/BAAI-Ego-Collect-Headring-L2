# Task library

此目录在部署后保存任务图片及参考图库内容，实际采集任务数据不进入 Git。

在手机页面“参考图库 → 从 ZIP 导入”导入任务包。任务包可以包含任务 PNG、
`_config.yaml`、`_placements.csv` 和 `_placements_overview.png`。
辅助文件随本段保存至 `reference_bundle/`；概览图不参与随机抽取。

初始化新工作站时使用 `selected_list_id=all`，导入后在界面选择需要采集的列表。
