# EgoRecord 设备硬件接入指南（相机 / IMU / 音频）

> **读者对象**：在 EgoRecord 设备（Orange Pi CM5, RK3588）上运行第三方采集/传输
> 程序的开发者。**无需项目源码**——本文自成一体，所有取数方法都给出了完整
> 可照抄的实现，照此可用任何语言对接。ROS 已停用，全部为直接接入方式。
>
> 前提：你的程序**不与 EgoRecord 录制程序同时运行**（见"统一约定"）。

## 1. 概述

### 设备上的数据

| 数据 | 硬件 | 接入入口 |
|------|------|----------|
| 4 路鱼眼相机视频 | NV12 1920x1200 @ 30fps，外触发硬件同步（分辨率以发布包 `config/` 下 camd.conf 的 width/height 为准） | **camd 守护进程**，Unix socket `/run/camd.sock`，单行 JSON 协议 |
| IMU（ICM20602） | 200Hz，内核字符设备 | 直接 `open('/dev/icm20602')` 读，无守护进程 |
| 双声道音频 | USB 麦克风（ALSA `hw:1,0`），48kHz S16_LE | `arecord` 子进程，无守护进程 |

### 版本要求（先看这个）

本文描述的 camd 接口（特别是 **STREAM_H264** 实时码流、归零的 MKV 时间轴）
要求设备安装的 EgoRecord **v0.1.9 或更高版本**。检查方法：

```bash
cat /opt/egorecord/VERSION     # 需 version=0.1.9 或更高
```

低于 v0.1.9 时 camd 接口不全：没有 STREAM_H264 命令（会收到 `{"error":...}`），
且 MKV 文件时间戳不从 0 起。运行时也可以探测：查 STATUS（见附录），
应答的每路相机里有 `h264_stream` 字段即为新版，缺该字段就是旧版 camd。
版本不够请联系设备提供方升级发布包。

**配置开关（新版 camd）**：STREAM_H264 与 STREAM_JPEG 都有 camd.conf 开关
（`h264_stream` / `jpeg_stream`，**默认均为 0 关闭**），未启用时命令返回
`{"error":"...未启用..."}`。设备上编辑 `/etc/egorecord/camd.conf` 置 1 并
`systemctl restart camd` 后生效。STATUS 应答顶层的
`h264_stream_enabled` / `jpeg_stream_enabled` 可查询当前开关状态。

### 统一约定（重要）

1. **同一时刻只允许一个程序使用硬件**。camd 每路推流只接受一个订阅者；
   IMU 字符设备只允许一个打开者；ALSA 录音设备同一时刻一个使用者。
2. **不要与 EgoRecord 录制程序同时运行**。接入前确认
   `systemctl is-active egorecord`（以及旧链路 `seeker_cam_bridge`）为 inactive。
3. **启动时先查 camd STATUS 做防御**：`state` 应为 `IDLE`，且目标路没有
   进行中的推流（见附录 STATUS 字段），否则说明有别的程序在用相机。
4. **用完清理**：推流发 `on:0`（或关闭连接）、关闭 IMU fd、停掉 arecord。
   异常退出时 camd 会因断连自动停止订阅，但正常路径请主动清理，
   避免下一个程序撞上"该路已在推流"。
5. **时间戳同域可对齐**：相机帧时间戳（CLOCK_REALTIME 微秒）与 IMU 时间戳
   （按本文方法换算后的 Unix 秒）在同一时钟域，可直接做跨传感器对齐。

## 2. 怎么拿数据（按场景）

### 公共部分：连接 camd 的 10 行助手

场景 A~D 都要先连 `/run/camd.sock`、收发单行 JSON。下面这个最小封装
（纯标准库，Python 3.6+）在后续示例中反复使用；关键是**自带接收缓冲**，
行读（JSON 应答）与二进制读（帧数据）才不会互相吃掉数据：

```python
import json, socket

class Conn:
    """camd socket 连接：send_cmd 发一行 JSON，read_line 读一行应答，
    read_exact 精确读 n 字节二进制。自带缓冲，两类读不冲突。"""

    def __init__(self, path='/run/camd.sock', timeout=5.0):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect(path)
        self.buf = bytearray()

    def send_cmd(self, payload):
        self.s.sendall((json.dumps(payload) + '\n').encode())

    def _fill(self):
        chunk = self.s.recv(65536)
        if not chunk:
            raise IOError('camd 关闭了连接')
        self.buf += chunk

    def read_line(self):
        while True:
            i = self.buf.find(b'\n')
            if i >= 0:
                line = bytes(self.buf[:i])
                del self.buf[:i + 1]
                return line
            self._fill()

    def read_exact(self, n):
        while len(self.buf) < n:
            self._fill()
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def close(self):
        self.s.close()
```

错误约定：任何命令失败时 camd 回一行 `{"error":"原因"}`；成功回
`{"ok":true,...}`（SNAPSHOT 成功时例外，直接发二进制，见场景 C）。

---

### 场景 A：要实时视频流（预览 / 往外转发）→ STREAM_H264

> 需在 camd.conf 设 `h264_stream=1`（默认关闭），见上文"配置开关"。

camd 的每路相机都有常驻运转的 H.264 硬件编码器，STREAM_H264 把编码出的
Annex-B 码流**连同精确时间戳**直接推给你，不封装 MKV，延迟极低
（10Mbps@30fps 约 40KB/帧）。**与录制不互斥**，每路一个订阅者。

码流性质（转发/落盘前要知道的）：

- **Annex-B byte-stream**（`00 00 00 01` 起始码），Baseline、无 B 帧，帧序即解码序；
- **每个 IDR 前自动带 SPS/PPS**，任意 IDR 处切入均可解码；
- **订阅即强制 IDR**：`on:1` 时 camd 强制下一帧出 IDR，推送从该 IDR 开始，
  所以你收到的**首帧一定是 flags bit0=1 的关键帧**，直接可解码；
- 客户端读得慢时 camd 丢**最旧**的帧（队列深度 16 ≈ 0.5s 缓冲），
  实时性优先于完整性；发送超时 1s 写不动会断开连接。

完整示例（抓 300 帧存成 .h264 文件，可直接 ffplay 验证）：

```python
#!/usr/bin/env python3
import struct
from conn import Conn          # 上文 10 行助手，或直接内联

HDR = struct.Struct('<IIIIq')  # 24 字节帧头
H264_MAGIC = 0x48323634        # 'H264'（线上字节为 ASCII "462H"）

c = Conn()
try:
    c.send_cmd({'cmd': 'STREAM_H264', 'cam': 'left', 'on': 1})
    reply = c.read_line()      # 首行必须是 {"ok":true}，否则含 error
    if b'"ok"' not in reply:
        raise SystemExit('订阅失败: %s' % reply.decode())

    with open('left.h264', 'wb') as f:
        for _ in range(300):   # 30fps 约 10 秒
            magic, plen, flags, _res, ts_us = HDR.unpack(c.read_exact(24))
            if magic != H264_MAGIC:
                raise SystemExit('帧头 magic 错误: 0x%08X' % magic)
            payload = c.read_exact(plen)
            is_keyframe = bool(flags & 0x1)
            f.write(payload)   # 原样拼接即是合法 .h264 裸流
            # ts_us: CLOCK_REALTIME 微秒；is_keyframe/payload 可转发
finally:
    try:
        c.send_cmd({'cmd': 'STREAM_H264', 'cam': 'left', 'on': 0})
        c.read_line()
    except Exception:
        pass                   # 直接关连接 camd 也会自动停订阅
    c.close()
```

24 字节帧头字段（小端，`struct '<IIIIq'`）：

| 偏移 | 字段 | 类型 | 说明 |
|------|------|------|------|
| 0  | `magic` | uint32 | 固定 `0x48323634`（'H264'；线上字节序为 ASCII "462H"），帧边界校验用 |
| 4  | `payload_len` | uint32 | 本帧 H.264 数据长度（字节），变长，必须用它读包体 |
| 8  | `flags` | uint32 | bit0 = keyframe：1 表示本帧是 IDR 关键帧 |
| 12 | `reserved` | uint32 | 保留，当前为 0 |
| 16 | `ts_us` | int64 | CLOCK_REALTIME 微秒（墙钟网格对齐），与 timestamps 文件同源；MKV 内 PTS 是单调时钟归零的相对时间轴，两者不同源 |

时间戳语义：由 v4l2 采集时间戳换算到 CLOCK_REALTIME（约为**曝光中间点**），
对齐到帧网格——30fps 时量化误差 ±16.7ms。与 timestamps 文件是同一个值；
MKV 文件内部的 PTS 则基于单调时钟（CLOCK_MONOTONIC）首帧归零，
不受 NTP 步进/slew 影响，跨源绝对时间同步请用本字段或 timestamps 文件。

ffmpeg 验证（首帧即带 SPS/PPS 的 IDR，无需额外参数）：

```bash
ffplay -f h264 -framerate 30 left.h264
ffmpeg -f h264 -framerate 30 -i left.h264 -c copy left.mp4   # 转封装验证完整性
# 转发场景：把 payload 按帧顺序写给下游即可（RTP 打包需自行按 NAL 切分）
```

退出时务必发 `on:0`（或直接关闭连接）。

---

### 场景 A2：要 JPEG 图像帧（自己编视频 / 网络传输）→ STREAM_JPEG

> 需在 camd.conf 设 `jpeg_stream=1`（默认关闭），见上文"配置开关"。

STREAM_JPEG 用 RK3588 内置 JPEG 编码核（jpege）把每帧编码成**完整独立的
JPEG 图像**推给你（q80 时 1920x1200 约 200-400KB/帧），等价于旧 ROS 链路
CompressedImage 的角色：帧内编码、无关键帧概念，任意时刻订阅即刻可解码，
直接写 `.jpg` 文件、`cv2.imdecode` 解码、或喂给你自己的编码管线都可以。

码流性质：

- **与录制、STREAM_RAW、STREAM_H264 均不互斥**（jpege 是独立硬件核），
  每路一个订阅者；
- **吞吐上限**：jpege 全芯片只有一颗，camd 内部全局单 session 串行编码。
  4 路全帧率（合计 120 帧/秒）可能跑不满；编不过来时 camd 丢**最旧**的帧
  不反压采集（录制不受影响）。帧率不够用 camd.conf 的 `jpeg_interval=2`
  抽帧（每路 15fps），画质用 `jpeg_quality`（1-100，默认 80）调；
- **带宽参考**：单路 q80@30fps 约 6-12MB/s，千兆网转发 4 路可达
  ~50MB/s，接近上限，建议只转发需要的路或抽帧；
- 丢帧检测：相邻帧 `ts_us` 间隔跳变（> 帧间隔 1.5 倍）即中间有丢帧。

帧头与 STREAM_H264 同构（24 字节，`struct '<IIIIq'`），仅 magic 为
`0x4A504547`（'JPEG'）、flags 保留为 0，详见附录"二进制帧头"。
用 `egorecord/camd_client.py` 的话直接 `client.stream_jpeg('left')`，
`read_frame()` 返回 `(ts_us, jpeg_bytes)`。

---

### 场景 B：要录像文件 → START/STOP 委托录制

最省事的方式：让 camd 自己录像。视频不经过你的进程，camd 内部完成
H.264 编码 → MKV 封装落盘 + 逐帧时间戳文件，断电也只损失尾部几秒
（MKV 流式写入 + timestamps 每 30 行 fsync）。

```python
from conn import Conn   # 上文助手；START/STOP 为短连接，各开各的

c = Conn(timeout=10.0)  # START 要等齐每路首个 IDR 才回 ok，超时要放宽
c.send_cmd({
    'cmd': 'START',
    'session_dir': '/media/orangepi/sdcard/data/my_task/session_001',
    'fps': 30,
    'bitrate_mbps': 10,
    'gop': 30,
})
print(c.read_line())    # {"ok":true} 或 {"error":"..."}
c.close()

# ... 录制中（可用 STATUS 查询进度）...

c = Conn(timeout=20.0)  # STOP 要封尾 MKV，可能耗时几秒
c.send_cmd({'cmd': 'STOP'})
print(c.read_line())    # {"ok":true,"cams":{"left":{"frames":N,...},...}}
c.close()
```

START 参数（字段名与 camd 实现一致）：

| 字段 | 必填 | 说明 |
|------|------|------|
| `session_dir` | 是 | 会话目录，camd 在其下自动建 `videos/` 与 `timestamps/` |
| `fps` | 否 | 1~120，默认取 camd.conf 值（30） |
| `bitrate_mbps` | 否 | 1~200，默认 10 |
| `gop` | 否 | 关键帧间隔（帧数），0~600；0 或缺省 = fps |

注意：camd 的编码器**常驻运转，参数以 camd.conf 为准**——START 附带的
fps/bitrate/gop 若与 camd.conf 不一致，camd 只告警、不重建编码器，
实际输出仍是 camd.conf 的参数。START 要等**每路**首个 IDR 后才回 ok
（超时 3s 自动回滚）；录制中再次 START 或录制时 STREAM_RAW 会被拒绝。

产物（`<session_dir>` 下）：

```
videos/left.mkv right.mkv bright.mkv bleft.mkv     # H.264 in Matroska
timestamps/left_timestamps.txt ...                 # 每路一个，逐帧时间戳
```

timestamps 文件每行 `frame_idx ts_seconds`（frame_idx 从 0 递增，
ts 为 Unix 秒、`%.9f` 精度，与 STREAM_H264 的 ts_us/1e6 同源）。

STOP 应答含每路统计：`frames`（写入帧数）、`first_ts_us`/`last_ts_us`、
`enc_errors`。

---

### 场景 C：要单张图片 → SNAPSHOT

任意状态可用（录制中也行），取该路最新一帧 NV12，超时 2s。
**应答没有 {"ok":true} 行**：成功直接发 24 字节头 + NV12 数据，
失败才回 JSON 错误行——按首字节是不是 `'{'` 区分。

```python
import json, struct
from conn import Conn

HDR = struct.Struct('<IIIIq')
NV12_MAGIC = 0x4E563132    # 'NV12'（线上字节为 ASCII "21VN"）

c = Conn()
c.send_cmd({'cmd': 'SNAPSHOT', 'cam': 'left'})
hdr = c.read_exact(24)
if hdr[0:1] == b'{':                       # JSON 错误行（读完剩余部分）
    rest = hdr + c.read_line()
    raise SystemExit('SNAPSHOT 失败: %s' % rest.decode())
magic, w, h, _res, ts_us = HDR.unpack(hdr)
assert magic == NV12_MAGIC
nv12 = c.read_exact(w * h * 3 // 2)        # NV12 = Y 平面 + 交错 UV，w*h*3/2 字节
c.close()

# NV12 → BGR → JPEG（需要 opencv；纯查看/存档用）
import numpy as np, cv2
img = np.frombuffer(nv12, dtype=np.uint8).reshape(h * 3 // 2, w)
bgr = cv2.cvtColor(img, cv2.COLOR_YUV2BGR_NV12)
cv2.imwrite('left.jpg', bgr)
```

NV12 布局：前 `w*h` 字节是 Y 平面，后 `w*h/2` 字节是 UV 交错（U、V 交替）。
自己解析时按 `reshape(h*3//2, w)` 看待即可。

---

### 场景 D：要原始帧做算法 → STREAM_RAW

逐帧推 NV12 原始像素（1920x1200 约 3.3MB/帧），供需要逐帧像素
（检测、标定、图像算法）的场景。**两个限制**：

- **与录制互斥**：RECORDING 状态下拒绝 STREAM_RAW，反之推流中拒绝 START；
- 每路单订阅；客户端读得慢时丢最旧帧（队列深度 4），发送超时 1s 断开。

```python
import struct
from conn import Conn

HDR = struct.Struct('<IIIIq')
NV12_MAGIC = 0x4E563132

c = Conn()
try:
    c.send_cmd({'cmd': 'STREAM_RAW', 'cam': 'left', 'on': 1})
    if b'"ok"' not in c.read_line():
        raise SystemExit('订阅失败（录制中？该路已被占用？）')

    while True:
        magic, w, h, _res, ts_us = HDR.unpack(c.read_exact(24))
        if magic != NV12_MAGIC:
            raise SystemExit('帧头 magic 错误')
        nv12 = c.read_exact(w * h * 3 // 2)
        process_frame(nv12, w, h, ts_us)   # 你的算法；慢了会被丢帧
finally:
    try:
        c.send_cmd({'cmd': 'STREAM_RAW', 'cam': 'left', 'on': 0})
        c.read_line()
    except Exception:
        pass
    c.close()
```

帧头与 SNAPSHOT 相同（magic `0x4E563132` 'NV12' + width + height + reserved
+ ts_us），详见附录。只要压缩码流不要像素时，优先用场景 A 的 STREAM_H264
（数据量小两个数量级，且不与录制互斥）。

---

### 场景 E：要 IMU 数据 → 直接读 /dev/icm20602

无守护进程，自己 open 读即可。**200Hz**，每次 read 返回 48 字节定长帧
（短读需自行累积拼帧）。设备只允许一个打开者。

```python
#!/usr/bin/env python3
import os, struct, math, time

FRAME = struct.Struct('<qq7i4x')   # 48 字节：2×int64 + 7×int32 + 4 字节填充
ACCEL_SCALE = 9.8 / 2048.0         # ADC → m/s²
GYRO_SCALE = (math.pi / 180.0) / 16.4   # ADC → rad/s

fd = os.open('/dev/icm20602', os.O_RDONLY)
ts_offset = None
buf = bytearray()
try:
    while True:
        buf.extend(os.read(fd, 48))
        while len(buf) >= 48:
            frame, buf = bytes(buf[:48]), buf[48:]
            (ts_sec, ts_nsec,
             gx, gy, gz, ax, ay, az, _temp) = FRAME.unpack(frame)

            # 时间戳：驱动在采样时刻打的 sec/nsec。若不是 Unix 实时钟
            #（如开机单调时钟，偏差超一天），首次采样记偏移换算回 Unix 时间
            raw = ts_sec + ts_nsec * 1e-9
            if ts_offset is None:
                ts_offset = 0.0 if abs(raw - time.time()) <= 86400 \
                            else time.time() - raw
            stamp = raw + ts_offset          # Unix 秒（float）

            gyro  = (gx * GYRO_SCALE, gy * GYRO_SCALE, gz * GYRO_SCALE)    # rad/s
            accel = (ax * ACCEL_SCALE, ay * ACCEL_SCALE, az * ACCEL_SCALE) # m/s²
            handle_imu(stamp, gyro, accel)   # 你的处理/转发，200Hz
finally:
    os.close(fd)
```

48 字节帧布局（小端）：

| 偏移 | 类型 | 字段 | 说明 |
|------|------|------|------|
| 0  | int64 | `timestamp_sec`  | 采样时刻秒（驱动时钟） |
| 8  | int64 | `timestamp_nsec` | 采样时刻纳秒 |
| 16 | int32 | `gyro_x_adc` | 陀螺仪 X 原始值 |
| 20 | int32 | `gyro_y_adc` | 陀螺仪 Y |
| 24 | int32 | `gyro_z_adc` | 陀螺仪 Z |
| 28 | int32 | `accel_x_adc` | 加速度计 X 原始值 |
| 32 | int32 | `accel_y_adc` | 加速度计 Y |
| 36 | int32 | `accel_z_adc` | 加速度计 Z |
| 40 | int32 | `temp_adc` | 温度 ADC（无标定换算，一般忽略） |
| 44 | 4 字节填充 | — | C 结构体对齐，无意义 |

补充：

- **姿态四元数硬件不输出**（EgoRecord 数据里恒 0）；只有陀螺仪 + 加速度计。
- **权限**：设备默认 `root:root 0600`；部署有 udev 规则
  （`KERNEL=="icm20602", MODE="0660", GROUP=<运行用户组>`），
  你的程序以该组用户运行即可，`ls -l /dev/icm20602` 可确认。
- 参考：EgoRecord 录制的 IMU CSV 为 11 列——
  `timestamp, orientation_x/y/z/w, angular_velocity_x/y/z, linear_acceleration_x/y/z`
  （timestamp 为 Unix 秒 float，orientation 恒 0，角速度 rad/s，加速度 m/s²，
  与 ROS `sensor_msgs/Imu` 字段同名）。

---

### 场景 F：要音频 → arecord 子进程

```bash
arecord -D usb_rec -c 2 -r 48000 -f S16_LE -t wav output.wav
```

- **设备名 `usb_rec`**：ALSA 别名（定义在 `/etc/asound.conf`，发布包已装）：
  `plug → dsnoop → hw:1,0`。USB 麦克风硬件固定 2ch S16_LE 16kHz，
  plug 层自动重采样，所以可以直接请求 48kHz。若确定独占也可用
  `-D hw:1,0 -r 16000`。
- 需要 MP3：录完用 `ffmpeg -i output.wav -codec:a libmp3lame -b:a 320k output.mp3`。
- 需要边录边流式外传：`arecord ... -t raw` 从 stdout 管道读 PCM 也行，
  但建议先落盘 WAV 再处理（简单可靠）。

**本平台的 arecord 坑（停止方式，重要）**：

- arecord 对 **SIGINT 无响应**；被信号终止时**不回写 WAV 头**
  （RIFF/data 长度停留在占位符，播放器显示 ~3.1 小时假时长）。
- 正确停法：发 **SIGTERM**，等进程退出后按实际文件大小修补 WAV 头：

```python
import os, struct

def patch_wav_header(path):
    size = os.path.getsize(path)
    with open(path, 'r+b') as f:
        f.seek(4)
        f.write(struct.pack('<I', min(size - 8, 0xFFFFFFFF)))   # RIFF 长度
        f.seek(40)
        f.write(struct.pack('<I', min(size - 44, 0xFFFFFFFF)))  # data 长度
```

Python 起停示例：`subprocess.Popen(['arecord', ...])` → 停止时
`p.terminate()`（即 SIGTERM）→ `p.wait(timeout=10)` → `patch_wav_header(...)`。

---

### 场景 G：组合采集（H264 + IMU + 音频同时）

> 用了 STREAM_H264，需 camd.conf `h264_stream=1`（默认关闭），见"配置开关"。

三路数据源互不冲突（相机走 camd socket、IMU 走字符设备、音频走 ALSA），
可以同时采集。流程要点：

1. **防御检查**：`STATUS` 确认 `state` 为 `IDLE`、目标路无推流占用。
2. **启动**：开 STREAM_H264 订阅（每路一条连接）→ `os.open` IMU →
   `Popen` arecord。三个入口独立，谁失败单独降级即可。
3. **采集循环**：视频在 `read_frame` 循环里拿（带 ts_us）；IMU 单开
   一个读取线程（或 select fd）200Hz 拿（带 Unix 秒时间戳）；
   音频由 arecord 落盘。时间戳同域（都是 CLOCK_REALTIME），
   后处理直接按时间轴对齐。
4. **退出清理**：`on:0` 停推流（或关连接）→ `os.close` IMU fd →
   arecord 发 SIGTERM → 等退出 → 修补 WAV 头。

```python
# 骨架（依赖场景 A/E/F 的代码片段，此处只串流程）
c = Conn()
c.send_cmd({'cmd': 'STATUS'});  assert b'"IDLE"' in c.read_line()
c.send_cmd({'cmd': 'STREAM_H264', 'cam': 'left', 'on': 1})
assert b'"ok"' in c.read_line()

imu_fd = os.open('/dev/icm20602', os.O_RDONLY)
arecord = subprocess.Popen(['arecord', '-D', 'usb_rec', '-c', '2',
                            '-r', '48000', '-f', 'S16_LE', '-t', 'wav', 'audio.wav'])
try:
    while True:
        hdr = c.read_exact(24)          # 视频帧（线程 1：H264 + ts_us）
        ...                              # IMU 解析循环（线程 2：场景 E 代码）
except KeyboardInterrupt:
    pass
finally:
    c.send_cmd({'cmd': 'STREAM_H264', 'cam': 'left', 'on': 0}); c.close()
    os.close(imu_fd)
    arecord.terminate(); arecord.wait(timeout=10)
    patch_wav_header('audio.wav')
```

## 3. 协议参考（附录）

### camd socket 与 JSON 约定

- 路径 `/run/camd.sock`（权限 0666，任意本地用户可连）。
- 每个请求是一行 JSON（`\n` 结尾）；应答是一行 JSON
  （SNAPSHOT 成功时除外：直接发二进制，无 JSON 应答行）。
- 失败统一回 `{"error":"原因"}`；推流类命令成功先回 `{"ok":true}`，
  随后同一连接转二进制帧流（长连接）。
- 相机名 `left` / `right` / `bright` / `bleft`，**以发布包 `config/` 下
  camd.conf 的 `cam=` 映射为准**。

### 命令速查

| 命令 | 参数 | 成功应答 | 说明 |
|------|------|----------|------|
| `STATUS` | — | `{"ok":true,"state":...,"cams":{...}}` | 查状态（接入前防御性检查用） |
| `START` | `session_dir`（必填）, `fps`, `bitrate_mbps`, `gop` | `{"ok":true}` | 委托录制，等齐每路首 IDR；与 STREAM_RAW 互斥 |
| `STOP` | — | `{"ok":true,"cams":{...统计...}}` | 停止录制，封尾 MKV |
| `SNAPSHOT` | `cam` | 24B 头 + NV12（无 JSON 行） | 单帧，任意状态可用，超时 2s |
| `STREAM_RAW` | `cam`, `on`:1/0 | 首行 `{"ok":true}` + 二进制帧流 | NV12 帧流；与录制互斥；每路单订阅 |
| `STREAM_H264` | `cam`, `on`:1/0 | 首行 `{"ok":true}` + 二进制帧流 | H.264 码流；不互斥；每路单订阅；订阅即强制 IDR；需 `h264_stream=1`（默认关） |
| `STREAM_JPEG` | `cam`, `on`:1/0 | 首行 `{"ok":true}` + 二进制帧流 | JPEG 图像帧；不互斥；每路单订阅；需 `jpeg_stream=1`（默认关） |

### 二进制帧头（均为 24 字节小端，`struct '<IIIIq'`）

NV12 帧头（SNAPSHOT / STREAM_RAW）：

| 偏移 | 字段 | 说明 |
|------|------|------|
| 0  | `magic` uint32 | 固定 `0x4E563132`（'NV12'；线上字节为 ASCII "21VN"） |
| 4  | `width` uint32 | 帧宽（默认 1920） |
| 8  | `height` uint32 | 帧高（默认 1200） |
| 12 | `reserved` uint32 | 0 |
| 16 | `ts_us` int64 | CLOCK_REALTIME 微秒 |

帧体 = `width*height*3/2` 字节 NV12（Y 平面 + 交错 UV）。

H264 帧头（STREAM_H264）：

| 偏移 | 字段 | 说明 |
|------|------|------|
| 0  | `magic` uint32 | 固定 `0x48323634`（'H264'；线上字节为 ASCII "462H"） |
| 4  | `payload_len` uint32 | 本帧 H.264 数据长度（变长） |
| 8  | `flags` uint32 | bit0 = keyframe（IDR） |
| 12 | `reserved` uint32 | 0 |
| 16 | `ts_us` int64 | CLOCK_REALTIME 微秒（墙钟网格对齐），与 timestamps 文件同源；MKV 内 PTS 为单调时钟归零的相对时间轴，两者不同源 |

帧体 = `payload_len` 字节 Annex-B H.264（每个 IDR 自带 SPS/PPS）。

JPEG 帧头（STREAM_JPEG）：

| 偏移 | 字段 | 说明 |
|------|------|------|
| 0  | `magic` uint32 | 固定 `0x4A504547`（'JPEG'；线上字节为 ASCII "GEPJ"） |
| 4  | `payload_len` uint32 | 本帧 JPEG 数据长度（变长） |
| 8  | `flags` uint32 | 保留，当前为 0（JPEG 帧内编码，无关键帧概念） |
| 12 | `reserved` uint32 | 0 |
| 16 | `ts_us` int64 | CLOCK_REALTIME 微秒（墙钟网格对齐），与 timestamps 文件同源 |

帧体 = `payload_len` 字节完整 JPEG 图像（SOI 到 EOI，可直接写 .jpg）。

### STATUS 应答字段

```json
{"ok":true, "state":"IDLE|RECORDING",
 "h264_stream_enabled":false, "jpeg_stream_enabled":true,
 "cams":{"left":{"frames_written":0, "fps":30.0,
                 "first_ts_us":0, "last_ts_us":0, "enc_errors":0,
                 "frames_dropped":0,
                 "snapshot_ready":true, "h264_stream":false,
                 "jpeg_stream":false, "jpeg_frames":0, "jpeg_dropped":0}, ...}}
```

- `state`：全局状态机；`IDLE` 才可 START，RECORDING 中拒绝 STREAM_RAW。
- `h264_stream_enabled` / `jpeg_stream_enabled`：camd.conf 的功能开关状态。
- 每路：`fps` 实时帧率、`snapshot_ready` 是否采到过帧、
  `h264_stream` / `jpeg_stream` 该路对应推流是否被占用（true 说明有别的程序在用）、
  `jpeg_frames` / `jpeg_dropped` 该路 JPEG 已推送帧数与队列满丢帧数。
- `frames_written`/`first_ts_us`/`last_ts_us`/`enc_errors`/`frames_dropped`
  为最近一次录制的统计（`frames_dropped` 为 v4l2 驱动侧丢帧数，START 时清零，
  由 buf.sequence 间隙累加；非零多为下游写盘 stall 反压）。

### 时间戳语义（相机）

`ts_us` 由 v4l2 采集时间戳（CLOCK_MONOTONIC）换算到 CLOCK_REALTIME，
参考点约为**曝光中间点**，再对齐到 RTC 整秒锚定的帧网格：
30fps 时量化误差 **±16.7ms**。与 timestamps 文件、IMU 换算后的 Unix
时间戳在同一时钟域，可直接跨传感器对齐。注意：MKV 文件内部的 PTS
自 2026-10 起改为单调时钟（CLOCK_MONOTONIC）首帧归零的相对时间轴，
不受 NTP 步进/slew 影响，**不再**与本字段同源；跨源对齐请以
timestamps 文件 / STREAM_H264 / STREAM_JPEG 的 ts_us 为准。

### 慢客户端与丢帧（推流通用）

每路订阅一个包队列（H264 深度 16 / RAW 深度 4 / JPEG 深度 4）；客户端读得慢
导致队列满时**丢最旧的帧**，实时性优先。发送超时 1s（SO_SNDTIMEO）；写失败
camd 停止该订阅并关闭连接。客户端断开连接，camd 自动停止对应订阅。
