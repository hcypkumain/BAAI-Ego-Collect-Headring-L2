# 主机回传、质检与归档服务源文件

这些文件来自当前运行的主机实现，配置/SSH 连接/HMAC 密钥、采集数据和虚拟环境不入库。部署时需要匹配的 `config.json`、`ssh/config`、`secrets/<device>`、collector 检查器和主机依赖环境，不可直接用示例覆盖现有主机。

`service.py` 接收封闭段，逐文件 SHA256 与落盘校验后在主机运行 QC，再回写对应 Pi 的结果，收到准确 ACK 后归档。清理必须按当前配置和签名回执执行，只针对完整核验的封闭段。

v4 回写前先调用 `prepare_results`，该动作校验设备、session、封闭状态和目标路径，避免新机器缺父目录导致 rsync 失败。已完成的 QC 报告及产物有哈希证明，Pi 忙时等待回写，不重复解码。

采集优先的空闲 guard 没有放宽：正在准备、录制、保存或仍有 active session 时，不从该 Pi 读取大文件、回写/清理。主机已接收的数据可以独立做 QC。当前 QC_LOCK 同时只允许一个段进入 QC；processor workers 不等于多个段同时解码。

`transfer-metrics.jsonl` 记录每次实际传输的字节、方向和耗时，用于估计真实文件吞吐，不把 RAM TCP 测速当成 TF 卡性能。
