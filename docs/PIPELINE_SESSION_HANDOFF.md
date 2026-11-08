# 保存到主机 Pipeline 的交接

v4 统一使用 `capture_session_state.active_session`：只要 worker 尚未回收，或流程仍处于准备、录制、等待保存/整理等非空闲状态，session 必须报告为 active；保存结束且 worker 回收后的 idle 不再暴露旧段为 active。

控制器内部的 `path`、`last_review_path`、`last_completed` 仍保留用于回看与重采，不通过删除历史路径来解除后台 guard。HTTP 和 controller.json 使用相同判据。

回写目录在新机/Git 安装时建立，且接收端每次回写前再次校验准备，不能依赖人工 mkdir。目录恢复不改变文件身份校验、归档 SHA256、结果 ACK 或捕获/删除锁。

默认建议分段保存并入队：空闲时传输，主机对已有副本独立质检，Pi 忙时暂停其读写与删除；日终处理剩余积压。本次未开启录制期间 TF 卡并发读/删，是否开启需要六路相机的实际压力测试与明确配置选择。
