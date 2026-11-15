# EM1003 环境宝3 蓝牙传感器 | BLE Sensor Integration

[![Version](https://img.shields.io/badge/version-2.0.0-blue.svg)](https://github.com/fooling/em1003)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-brightgreen.svg)](https://www.home-assistant.io/)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

[English](#english) | [中文](#中文)

---

## 中文

### 概述

这是一个专为 **720环境宝3（EM1003）** 打造的 Home Assistant 蓝牙（BLE）自定义集成组件。
版本 **2.0.0** 实现了全面的逆向工程协议支持，不仅涵盖 8 项核心空气质量指标，还新增了电池电量、充电状态、蜂鸣器控制、Wi-Fi 与云端通讯状态、外接 USB 供电状态以及蓝牙即插即用自动发现（Auto Discovery）。

### 1. 特色功能

- 🔍 **蓝牙即插即用自动发现（Auto Discovery）**：支持匹配 `3GC*`、`CG3*`、`EM1003*`、`720*` 广播名称。设备开机后，Home Assistant 的「设备与服务」会自动弹出发现卡片，一键即可配对完成。
- 📊 **8 项核心空气质量监测**：温度、湿度、噪音、PM2.5、PM10、甲醛、TVOC、eCO2，均采用经过逆向工程验证的 Little Endian 解码与校准公式。
- 🔋 **电池与供电监测**：支持电池电量百分比（0-100%）、电池充电中状态、以及外接 USB 电源接通状态。
- 🔔 **蜂鸣器控制（Switch）**：支持在 Home Assistant 中直接开启/关闭环境宝的报警蜂鸣器（注：原厂固件在设备每次重启时蜂鸣器默认开启）。
- 📶 **Wi-Fi 与云端状态监测**：即使官方云端停服，也能实时读取设备内部的 Wi-Fi 连接状态代号、云端上报通道状态，并提供激活 Wi-Fi 网络的快捷按钮。
- 🛡️ **高可靠性连接架构**：
  - **单会话批量读取**：每次轮询在同一 BLE 连接会话中依次读取所有数据并立即断开，杜绝蓝牙连接槽位（Connection Slot）耗尽问题。
  - **断路器（Circuit Breaker）**：遇到信号微弱或通讯异常时自动熔断并退避，保护主机资源与设备电池。
  - **传感器预热保护**：自动过滤开机预热阶段的异常负温与零值，避免历史图表被脏数据污染。
- 🏷️ **多版本硬件智能识别与设备信息呈现**：
  - 自动通过蓝牙读取内部存储的序列号（SN）、PlaceID 与 Wi-Fi / BLE 固件版本。
  - 厂牌统一规范呈现为 `720`。
  - 型号自动识别并呈现为特定定制版本：如 `EM1003 (HiLink)`（华为版）、`EM1003 (JD)`（京东版）、`EM1003 (720)`（原版）、`EM1003S (Samsung)`（三星版）等。
  - 固件版本完整显示 Wi-Fi 与 BLE 双模块固件号（如 `1.2.0 (BLE: 1.1.2)`）。
- 🗂️ **标准实体分类与整洁排版**：严格遵循 Home Assistant 官方规范，核心指标、配置（CONFIG）、诊断（DIAGNOSTIC）分类清晰；诊断实体默认停用，避免污染主界面。

---

### 2. 支持的实体清单

| 实体 | 实体 ID / 类别 | 分类 (Category) | 默认状态 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| **温度** | `sensor.*_temperature` | 主感测器 | **启用** | °C，校准公式 `(raw - 4000) / 100` |
| **湿度** | `sensor.*_humidity` | 主感测器 | **启用** | %，公式 `raw / 100` |
| **噪音** | `sensor.*_noise` | 主感测器 | **启用** | dB，实时环境噪音分贝 |
| **PM2.5** | `sensor.*_pm25` | 主感测器 | **启用** | µg/m³，细颗粒物浓度 |
| **PM10** | `sensor.*_pm10` | 主感测器 | **启用** | µg/m³，可吸入颗粒物浓度 |
| **甲醛** | `sensor.*_formaldehyde` | 主感测器 | **启用** | mg/m³，公式 `(raw - 16384) / 1000` |
| **TVOC** | `sensor.*_tvoc` | 主感测器 | **启用** | µg/m³，总挥发性有机物 |
| **eCO2** | `sensor.*_eco2` | 主感测器 | **启用** | ppm，等效二氧化碳浓度 |
| **电池电量** | `sensor.*_battery` | 主感测器 | **启用** | %，0~100% 剩余电量 |
| **蜂鸣器开关** | `switch.*_buzzer` | 设定 (CONFIG) | **启用** | 控制设备蜂鸣器开/关 |
| **激活 Wi-Fi 按钮** | `button.*_activate_wifi` | 设定 (CONFIG) | *停用* | 发送 `0x2E` 激活已配置的 Wi-Fi 档案 |
| **电池充电中** | `binary_sensor.*_battery_charging` | 诊断 (DIAGNOSTIC) | **启用** | 接通电源且处于充电状态时为 `on` |
| **外接 USB 供电** | `binary_sensor.*_usb_power` | 诊断 (DIAGNOSTIC) | *停用* | USB 外接电源接通时为 `on` |
| **Wi-Fi 连线状态** | `binary_sensor.*_wifi_connected` | 诊断 (DIAGNOSTIC) | *停用* | 连线成功时为 `on` |
| **Wi-Fi 详细状态** | `sensor.*_wifi_status` | 诊断 (DIAGNOSTIC) | *停用* | 状态值：`connected`, `connecting`, `idle`, `ssid_not_found` 等 |
| **云端上报状态** | `binary_sensor.*_cloud_reporting` | 诊断 (DIAGNOSTIC) | *停用* | 云端通道畅通时为 `on` |
| **云端详细状态** | `sensor.*_cloud_status` | 诊断 (DIAGNOSTIC) | *停用* | 状态值：`connected`, `disconnected`, `unknown` 等 |

> **提示**：标注为 *停用* 的实体遵循 HA 最佳实践，初始添加到注册表中但默认不启用。若需要使用，只需在 Home Assistant 设备页面点击该实体并选择「启用」即可。

---

### 3. 安装方式

#### 通过 HACS 安装（推荐）

1. 在 Home Assistant 中打开 **HACS**。
2. 点击右上角 **⋮** 菜单，选择 **自定义存储库 (Custom repositories)**。
3. 填写存储库地址：`https://github.com/fooling/em1003`，类别选择 `Integration`（集成），点击 **添加**。
4. 在 HACS 列表中搜索 **"EM1003"**，点击进入并点击右下角 **下载** 最新版本（2.0.0）。
5. **重启 Home Assistant**。
6. 重启后：
   - **自动发现**：若设备在附近且开机，HA「设备与服务」会自动弹出通知，点击确认即可。
   - **手動新增**：进入 **设置 > 设备与服务 > 添加集成**，搜索 **"EM1003"**，从下拉框选择扫描到的环境宝设备即可。

#### 手动安装

复制 `custom_components/em1003` 到 Home Assistant 的 `config/custom_components` 目录，重启 Home Assistant 即可。

---

### 4. 协议逆向工程核心原理

#### BLE 通信架构
```
服务 UUID: 09de2880-1415-4e2c-a48a-3938e3288537
  ├─ 写特征 0xFFF1: 发送命令（WRITE without response）
  └─ 通知特征 0xFFF4: 接收响应数据（NOTIFY）
```

#### 指令集总览

| 功能 | 请求格式 | 响应格式 | 备注 |
| :--- | :--- | :--- | :--- |
| **读取传感器** | `[seq] 0x06 [sensor_id]` | `[seq] 0x06 [sensor_id] [低位] [高位]` | Little Endian，两字节无符号整数 |
| **读取蜂鸣器状态** | `[seq] 0x50 0x00` | `[seq] 0x50 0x00 [0x00关/0x01开]` | 查询当前蜂鸣器开关状态 |
| **设置蜂鸣器** | `[seq] 0x50 0x01 [0x00关/0x01开]` | `[seq] 0x05 0x01` | 设置后附带回读校验 |
| **读取 Wi-Fi/云端状态** | `[seq] 0x19 0x00` | `[seq] 0x19 [组数] [当前索引] [Wi-Fi状态] [云端状态] [扫描标志]` | 完整解析 Wi-Fi 与云端网络状况 |
| **读取 USB 供电状态** | `[seq] 0x26 0x00` | `[seq] 0x26 [0x00电池/0x01外接USB]` | 判断外接供电状态 |
| **激活 Wi-Fi 网络** | `[seq] 0x2E [profile_index]` | `[seq] 0x2E` | 触发设备连接已存储的 Wi-Fi 档案 |

完整协议细节请参阅：[docs/protocol_reverse_engineering.md](docs/protocol_reverse_engineering.md)。

---

### 5. 扩展蓝牙覆盖范围：ESP32 蓝牙代理

若 Home Assistant 主机距离环境宝较远，推荐使用廉价的 ESP32（如 ESP32-C3、ESP32 Supermini）制作 **Bluetooth Proxy**：

1. 浏览器访问 [ESPHome Bluetooth Proxy 官方项目](https://esphome.io/projects/)。
2. 将 ESP32 插入电脑 USB，点击 **"Install"** 烧录固件。
3. 烧录完成时按提示输入家庭 Wi-Fi 密码。
4. ESP32 接入网络后，Home Assistant 会自动发现并将其作为蓝牙代理。环境宝3的广播将被透明转发至 HA，实现全屋覆盖。

---

## English

### Overview

A comprehensive Home Assistant custom integration for the **EM1003 BLE Sensor** (720 Air Quality Monitor 3).
Version **2.0.0** introduces a fully reverse-engineered protocol implementation that supports 8 core environmental metrics, battery level and charging status, buzzer configuration, Wi-Fi and cloud connection diagnostic sensors, external USB power detection, and seamless Bluetooth Auto-Discovery.

### 1. Key Features

- 🔍 **Bluetooth Auto-Discovery**: Matches broadcast local names (`3GC*`, `CG3*`, `EM1003*`, `720*`). Nearby powered-on devices automatically appear as discovered on the Home Assistant Devices page.
- 📊 **8 Core Air Quality Metrics**: Temperature, Humidity, Noise, PM2.5, PM10, Formaldehyde, TVOC, and eCO2 using confirmed Little Endian formulas.
- 🔋 **Battery & Power Management**: Battery level percentage (0-100%), charging binary sensor, and USB external power detection.
- 🔔 **Buzzer Control Switch**: Turn the internal alert buzzer on or off directly from Home Assistant (firmware defaults buzzer to ON after device reboot).
- 📶 **Wi-Fi & Cloud Diagnostics**: Monitor internal Wi-Fi connection states, cloud reporting status codes, and trigger Wi-Fi network profile activation.
- 🛡️ **Robust Architecture**:
  - **Single-Session Batch Polling**: Reads all sensors, battery, buzzer, and Wi-Fi status within a single BLE connection session, preventing BLE connection slot exhaustion.
  - **Circuit Breaker**: Protects the device and Home Assistant host from Bluetooth hangs with automatic backoff.
  - **Warmup Outlier Rejection**: Discards cold-boot negative temperatures and sensor warmup artifacts.
- 🏷️ **Hardware Variant & Firmware Detection**:
  - Automatically reads Serial Number (SN), PlaceID, and BLE / Wi-Fi firmware versions via Bluetooth.
  - Device Manufacturer is standardized as `720`.
  - Device Model dynamically identifies hardware editions: `EM1003 (HiLink)` (Huawei), `EM1003 (JD)` (Jingdong), `EM1003 (720)` (Standard), `EM1003S (Samsung)` (Samsung Connect), etc.
  - Displays dual-module firmware version (e.g. `1.2.0 (BLE: 1.1.2)`).
- 🗂️ **Standard Entity Categories**: Properly categorized into Primary, Configuration, and Diagnostic; diagnostic and network entities are disabled by default to maintain a clean dashboard.

---

### 2. Supported Entities

| Entity | Entity ID / Domain | Category | Default State | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Temperature** | `sensor.*_temperature` | Primary | **Enabled** | °C, formula `(raw - 4000) / 100` |
| **Humidity** | `sensor.*_humidity` | Primary | **Enabled** | %, formula `raw / 100` |
| **Noise** | `sensor.*_noise` | Primary | **Enabled** | dB, real-time ambient sound level |
| **PM2.5** | `sensor.*_pm25` | Primary | **Enabled** | µg/m³, fine particulate matter |
| **PM10** | `sensor.*_pm10` | Primary | **Enabled** | µg/m³, respirable particulate matter |
| **Formaldehyde** | `sensor.*_formaldehyde` | Primary | **Enabled** | mg/m³, formula `(raw - 16384) / 1000` |
| **TVOC** | `sensor.*_tvoc` | Primary | **Enabled** | µg/m³, total volatile organic compounds |
| **eCO2** | `sensor.*_eco2` | Primary | **Enabled** | ppm, equivalent carbon dioxide |
| **Battery Level** | `sensor.*_battery` | Primary | **Enabled** | %, 0-100% remaining charge |
| **Buzzer Switch** | `switch.*_buzzer` | Configuration | **Enabled** | Turn internal buzzer ON or OFF |
| **Activate Wi-Fi Button** | `button.*_activate_wifi` | Configuration | *Disabled* | Send `0x2E` to reconnect saved Wi-Fi |
| **Battery Charging** | `binary_sensor.*_battery_charging` | Diagnostic | **Enabled** | `on` when connected to power and charging |
| **USB Power Connected** | `binary_sensor.*_usb_power` | Diagnostic | *Disabled* | `on` when external USB cable is plugged in |
| **Wi-Fi Connected** | `binary_sensor.*_wifi_connected` | Diagnostic | *Disabled* | `on` when device Wi-Fi connection succeeds |
| **Wi-Fi Status** | `sensor.*_wifi_status` | Diagnostic | *Disabled* | State: `connected`, `connecting`, `idle`, `ssid_not_found` |
| **Cloud Reporting** | `binary_sensor.*_cloud_reporting` | Diagnostic | *Disabled* | `on` when cloud reporting channel is active |
| **Cloud Status** | `sensor.*_cloud_status` | Diagnostic | *Disabled* | State: `connected`, `disconnected`, `unknown` |

---

### 3. Installation

#### Via HACS (Recommended)

1. Open **HACS** in Home Assistant.
2. Click **⋮** (top right) > **Custom repositories**.
3. Add repository URL `https://github.com/fooling/em1003` with category `Integration`.
4. Search for **"EM1003"** in HACS and download version 2.0.0.
5. **Restart Home Assistant**.
6. After restart, the device will be automatically discovered in **Settings > Devices & Services**, or you can add it manually from the integration list.

#### Manual Installation

Copy `custom_components/em1003` into your Home Assistant `config/custom_components/` directory and restart Home Assistant.

---

### 4. Protocol Reference

#### BLE GATT Architecture
```
Service UUID: 09de2880-1415-4e2c-a48a-3938e3288537
  ├─ Write Char 0xFFF1: Send commands (WRITE without response)
  └─ Notify Char 0xFFF4: Receive responses (NOTIFY)
```

#### Protocol Commands

| Feature | Request | Response | Details |
| :--- | :--- | :--- | :--- |
| **Read Sensor** | `[seq] 0x06 [sensor_id]` | `[seq] 0x06 [sensor_id] [low] [high]` | Little Endian 16-bit unsigned int |
| **Query Buzzer** | `[seq] 0x50 0x00` | `[seq] 0x50 0x00 [0x00/0x01]` | `0x00`=OFF, `0x01`=ON |
| **Set Buzzer** | `[seq] 0x50 0x01 [state]` | `[seq] 0x05 0x01` | Sets state and verifies via query |
| **Read Wi-Fi / Cloud** | `[seq] 0x19 0x00` | `[seq] 0x19 [profiles] [idx] [wifi_state] [cloud_state] [scan_done]` | Detailed network status |
| **Read USB Power** | `[seq] 0x26 0x00` | `[seq] 0x26 [0x00/0x01]` | `0x01`=External USB power connected |
| **Activate Wi-Fi** | `[seq] 0x2E [profile_idx]` | `[seq] 0x2E` | Triggers Wi-Fi connection |

See [docs/protocol_reverse_engineering.md](docs/protocol_reverse_engineering.md) for full protocol analysis.

---

### 5. Range Extension with ESP32 Bluetooth Proxy

If your Home Assistant host is far from the EM1003 monitor, use an ESP32 as an [ESPHome Bluetooth Proxy](https://esphome.io/projects/):
1. Plug an ESP32 into a computer and open the web installer.
2. Select **"Bluetooth Proxy"** and click **Install**.
3. Configure your Wi-Fi credentials when prompted.
4. Home Assistant will discover the proxy and transparently forward EM1003 Bluetooth advertisements over Wi-Fi.

---

### License

MIT License - See [LICENSE](LICENSE)
