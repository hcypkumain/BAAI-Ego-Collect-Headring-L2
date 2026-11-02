# SC233HGS IQ 调优配置

适用: 头戴式第一人称多相机录制系统 (Orange Pi CM5 / RK3588)

## 改动摘要

| 项目 | 原始 | 调优后 | 目的 |
|---|---|---|---|
| AE Route | 6档, 1/100~1/50s, ISO 50~200 | 32档, 1/7037~1/150s, 固定ISO 50 | 减运动模糊 |
| AE 策略 | LOWLIGHT | HIGHLIGHT | 防过曝 |
| 目标亮度 | [45,45,40,38,33,30] | [35,35,32,30,28,25] | 保留高光余量 |
| DRC IIR/damp | 2/0.9 | 1/0.7 | 减拖影 |
| TNR ISO50 lo/hi | 1.2 | 1.0 | 减鬼影 |
| TNR clipwgt | 0.032 | 0.05 | 加快运动检测 |
| YNR ISO50 | 0.7/0.85 | 0.5/0.6 | 保留细节 |
| 锐化 sharp_ratio | 5 | 8 | 增强清晰度 |
| debayer sharp | 4 | 6 | 增强去马赛克细节 |
| 对比度/饱和度 | 128 | 135 | 补偿偏暗画面 |
| amerge damp | 0.9 | 0.7 | 减HDR合并拖影 |

## 使用

```bash
# 部署新 IQ (自动备份原始文件)
sudo bash switch_iq.sh deploy

# 查看状态
sudo bash switch_iq.sh status

# 回退到原始 IQ
sudo bash switch_iq.sh restore
```

## 注意

- 固定 ISO 50 + 最慢快门 1/150s，极暗环境画面可能偏暗
- 时域处理 (TNR/DRC/amerge) 已减弱，减少鬼影但可能轻微增加噪点
- 4 个相机白平衡独立运行，色温可能略有差异（正常现象）
