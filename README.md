# MiWiFi Router - Home Assistant Custom Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/tiejiang29/miwifi_router)](https://github.com/tiejiang29/miwifi_router)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2023.1%2B-blue)](https://www.home-assistant.io/)

小米 MiWiFi 路由器 Home Assistant 自定义集成，通过路由器本地 Web API 获取实时数据，无需云端。

## ✨ 功能特性

### 📊 路由器传感器 (Sensor)

| 传感器 | 说明 | 单位 |
|--------|------|------|
| Download Speed | WAN 下载速率 | B/s |
| Upload Speed | WAN 上传速率 | B/s |
| Download Total | 累计下载量 | B |
| Upload Total | 累计上传量 | B |
| Online Devices | 在线设备数 | 台 |
| CPU Load | CPU 负载 | % |
| Memory Usage | 内存使用率 | % |

### 📱 设备追踪 (Device Tracker)

- 每个连接设备自动创建 `device_tracker` 实体
- 在线/离线状态实时检测（`home` / `not_home`）
- **单设备速率数据**：
  - `upload_speed` / `download_speed`：实时上传/下载速率
  - `upload_total` / `download_total`：累计流量
  - `max_upload_speed` / `max_download_speed`：峰值速率
- 信号强度、频道、OUI 等额外信息

### 📶 设备传感器 (Per-Device Sensor)

> 🆕 v1.3.0 新增

在集成配置中手动选择需要监控的设备，为每个选中设备自动创建 **4 个独立传感器**：

| 传感器 | 说明 | 单位 |
|--------|------|------|
| {设备名} Download Speed | 设备下载速率 | B/s |
| {设备名} Upload Speed | 设备上传速率 | B/s |
| {设备名} Download Total | 设备累计下载量 | B |
| {设备名} Upload Total | 设备累计上传量 | B |

**与 device_tracker 的区别**：
- `device_tracker` 的速率数据是属性（attribute），**不记录历史**，无法在图表中展示
- `device_sensor` 是独立传感器实体，**支持历史记录**，可直接用于 mini-graph-card 等图表卡片

**特性**：
- 新建条目时可选择设备，也可跳过稍后配置
- 已离线但之前选中的设备标注 `[离线]`，方便重新选中
- 取消勾选的设备，其传感器实体在重载后自动清理

### ⚡ 性能优化

| 优化策略 | 说明 | 效果 |
|---------|------|------|
| **分层轮询** | 实时数据 10s + 设备列表 30s + 静态信息 5min | 请求量减少 60%+ |
| **智能触发** | 在线设备数变化时立即轮询设备列表 | 离线检测延迟 < 10秒 |
| **stok 缓存** | 登录 token 缓存 10 分钟 | 减少认证开销 |
| **HTTP Keep-Alive** | httpx 连接池复用 TCP 连接 | 每次节省 ~50ms |

## 📋 支持型号

所有运行 MiWiFi 固件的小米路由器，包括但不限于：

| 型号 | 平台 | 状态 |
|------|------|------|
| Xiaomi BE5000 | RD18 | ✅ 已验证 |
| Xiaomi BE3600 | RD09 | ✅ 兼容 |
| Xiaomi AX9000 | RA69 | ✅ 兼容 |
| Xiaomi AX6000 | RA67 | ✅ 兼容 |
| Xiaomi AX3600 | RA70 | ✅ 兼容 |
| Xiaomi AX3000T | RB01 | ✅ 兼容 |
| Redmi AX6 | RA69 | ✅ 兼容 |
| Redmi AX5 | RB03 | ✅ 兼容 |
| Redmi AC2100 | RM2100 | ✅ 兼容 |

> 其他 MiWiFi 固件路由器理论上均兼容，欢迎反馈测试结果。

## 📥 安装

### 方法 1：HACS（推荐）

1. 在 HACS 中添加自定义仓库：
   - 打开 HACS → 集成 → 右上角菜单 → 自定义仓库
   - 仓库 URL：`https://github.com/tiejiang29/miwifi_router`
   - 类别：集成
2. 搜索 "MiWiFi Router" 并点击安装
3. 重启 Home Assistant

### 方法 2：手动安装

1. 下载 `custom_components/miwifi_router/` 目录
2. 复制到 HA 配置目录的 `custom_components/` 下
```bash
cp -r custom_components/miwifi_router /config/custom_components/
```
3. 重启 Home Assistant

## ⚙️ 配置

### 新建条目

通过 UI 配置流程完成，**无需编辑 YAML**：

1. 进入 **设置 → 设备与服务 → 添加集成**
2. 搜索 "MiWiFi Router"
3. 填写连接信息：
   - **路由器 IP 地址**：默认 `192.168.31.1`
   - **路由器管理密码**：MiWiFi 管理界面登录密码
   - **实时数据轮询间隔**：默认 10 秒
   - **设备列表轮询间隔**：默认 30 秒
4. 连接成功后，进入 **选择需添加传感器的设备** 步骤：
   - 勾选需要监控的设备（可跳过，稍后添加）
   - 每个选中设备将创建 4 个独立传感器
5. 提交完成

### 修改配置

1. 进入 **设置 → 设备与服务 → MiWiFi Router → ⚙️ 配置**
2. 在 **需添加传感器的设备** 下拉框中勾选/取消设备
3. 提交后集成自动重载，传感器立即生效

## 📖 使用示例

### 设备速率图表

使用 mini-graph-card 展示设备实时速率：

```yaml
type: custom:mini-graph-card
entities:
  - sensor.xiaomizhu_lu_you_qi_istoreos_download_speed
  - sensor.xiaomizhu_lu_you_qi_istoreos_upload_speed
name: istoreos 网速
hours_to_show: 1
```

### 自动化：设备上线通知

```yaml
automation:
  - alias: "手机连接WiFi通知"
    trigger:
      - platform: state
        entity_id: device_tracker.miwifi_router_device_xx_xx_xx_xx_xx_xx
        to: "home"
    action:
      - service: notify.mobile_app
        data:
          title: "设备上线"
          message: "{{ trigger.to_state.attributes.friendly_name }} 已连接WiFi"
```

### 自动化：设备离线检测

```yaml
automation:
  - alias: "设备离线通知"
    trigger:
      - platform: state
        entity_id: device_tracker.miwifi_router_device_xx_xx_xx_xx_xx_xx
        to: "not_home"
        for:
          minutes: 5
    action:
      - service: notify.mobile_app
        data:
          title: "设备离线"
          message: "{{ trigger.from_state.attributes.friendly_name }} 已断开WiFi超过5分钟"
```

### 查看设备速率（device_tracker 属性）

```yaml
# 在模板中使用
{{ state_attr('device_tracker.miwifi_router_device_xx_xx_xx_xx_xx_xx', 'download_speed_human') }}
# 输出: "2.45 MB/s"

{{ state_attr('device_tracker.miwifi_router_device_xx_xx_xx_xx_xx_xx', 'upload_speed_human') }}
# 输出: "156.00 KB/s"
```

## ❓ 常见问题

### Q: 传感器显示"未知"？
检查路由器 IP 和密码是否正确，确保 HA 和路由器在同一局域网。可在配置中点击"验证"测试连接。

### Q: 为什么不用长连接/WebSocket？
小米路由器的 `uhttpd` 服务器不支持 WebSocket、SSE 或长轮询。路由器管理界面本身也是 2-5 秒轮询。10 秒轮询间隔对家庭自动化完全够用。

### Q: 和 xiaomi_home / hass-xiaomi-miot 有什么区别？
那两个集成走 MIoT 协议，但路由器固件不响应属性读取请求，所以传感器全部显示"未知"。本集成走路由器本地 Web API，直接获取实时数据。

### Q: 会影响路由器性能吗？
不会。HTTP Keep-Alive 复用连接，分层轮询减少请求量。路由器管理界面每 2 秒轮询，本集成默认 10 秒，负载远低于网页管理。

### Q: device_tracker 的速率数据和设备传感器有什么区别？
`device_tracker` 的速率是属性（attribute），HA 不记录属性历史，无法直接画图。设备传感器是独立实体，支持历史记录，可直接用于图表。如果需要在仪表盘中展示设备速率曲线，请选中对应设备创建传感器。

## 📁 文件结构

```
custom_components/miwifi_router/
├── __init__.py          # 集成入口
├── api.py               # 路由器 API 客户端（登录、stok缓存、Keep-Alive）
├── config_flow.py       # UI 配置流程（含设备选择步骤）
├── const.py             # 常量定义
├── coordinator.py       # 数据更新协调器（分层轮询策略）
├── device_tracker.py    # 设备追踪平台（在线检测+单设备速率属性）
├── manifest.json        # 集成清单
├── sensor.py            # 传感器平台（路由器+可选设备传感器）
└── strings.json         # 配置流程翻译
```

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## ⚖️ 许可证

[MIT License](LICENSE)
