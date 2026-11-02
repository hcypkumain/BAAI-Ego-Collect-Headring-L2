# 手机媒体服务迁移

此次修改清理的是仓库。手机预览、参考图缩放、历史回看和耳机控制仍需要本机媒体后端，
因此这些共享功能保留到 `services/phone-media/`，并使用 `pi-phone-media.service`。

已移除旧终端缩略图 API、旧双腕校准 IPC、固定直连网段白名单、旧 unit/drop-in 和界面入口文案。
单路腕相机的高帧率通道仍保留，新的共享内存目录是 `/dev/shm/pi-collect-wrist-preview`。
手机腕相机标定原本通过采集控制器的文件流实现，继续使用原有手机流程。

## 已有 Pi 的切换步骤

1. 结束当前录制、准备和后台上传，备份当前代码与 unit。仓库清理不会自动修改设备。
2. 在空闲时停止采集控制器和旧媒体服务，避免旧/新进程占用同一个 8766 端口：

   ```bash
   sudo systemctl stop embodied-pi-collect
   sudo systemctl disable --now rv06-preview.service
   ```

3. 更新完整仓库后，运行 `scripts/install_pi_services.py --apply`。
   不要只拷贝新的媒体后端：生产者与消费者必须同时使用新的共享内存目录。
4. 耳机管理仍使用本机用户的私有 PulseAudio socket。
   对照 `deployment/os-reference/pulse/default.pa`，将旧 socket 配置替换为
   `/run/user/1000/pulse/pi-collect-headphones`；保持目录权限 0700。
   按设备当前音频管理方式重载该配置，勿在录制中重启音频。
5. 启用新媒体服务，再启动采集控制器：

   ```bash
   sudo systemctl enable --now pi-phone-media
   sudo systemctl start embodied-pi-collect
   systemctl status pi-phone-media embodied-pi-collect --no-pager
   ```

6. 手机整页刷新；如打开独立参考图库页面，重新从 Pi 采集控制页获取管理码。
   检查六路预览、参考图放大、历史回看和耳机控制后，再开始正式采集。

新服务的 8766 端口仅监听 `127.0.0.1`；手机仍访问 `http://<Pi-IP>:8765/phone`。
原快照清单中保留旧目录名用于来源追踪，不参与新服务部署。
