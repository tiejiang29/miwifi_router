# MiWiFi Router - Home Assistant Custom Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/tiejiang29/miwifi_router)](https://github.com/tiejiang29/miwifi_router)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2023.1%2B-blue)](https://www.home-assistant.io/)

小米 MiWiFi 路由器 Home Assistant 自定义集成，通过路由器本地 Web API 获取实时数据，无需云端。

## ✨ 功能特性

### 📊 路由器传感器 (Sensor)

| 传感器 | 说明 | 默认单位 | 可选单位 |
|--------|------|---------|---------|
| Download Speed | WAN 下载速率 | B/s | B/s, kB/s, MB/s, GB/s, KiB/s, MiB/s, GiB/s |
| Upload Speed | WAN 上传速率 | B/s | 同上 |
| Download Total | 累计下载量 | B | B, kB, MB, GB, TB, KiB, MiB, GiB, TiB |
| Upload Total | 累计上传量 | B | 同上 |
| Online Devices | 在线设备数 | devices | (固定) |
| CPU Load | CPU 负载 | % | (固定) |
| Memory Usage | 内存使用率 | % | (固定) |

> 🆕 **v1.4.0 用户可选单位 + 中文本地化 + 修改确认**
> - 在集成配置里新增 **`speed_unit`**（实时速度单位）和 **`total_unit`**（累计流量单位）两个下拉选项
> - 默认 `Auto` = 跟之前一样用 B/s 和 B（最大兼容性，能量面板/长期统计完全不受影响）
> - 选其他单位（如 MB/s, GB）后，`native_unit_of_measurement` 改为对应单位，`native_value` 自动换算
> - 单位切换会**自动触发实体重建**（HA 限制：state_class 实体不能动态改 native_unit），相关实体的**历史状态数据会丢失**
> - 修改单位时会弹出**确认对话框**，明确警告历史数据丢失，用户必须勾选确认才生效
> - 长期统计（statistics 表）和能量面板不受影响，新数据按新单位继续累计
> - 任何情况下都保留 `raw_b` 属性（原始字节数）和 `human_readable` 属性（友好字符串）
> - 完整中文本地化：所有标题、描述、字段标签、实体名都中文化（基于 `translations/zh-Hans.json`）
> 
> **历史变更说明**：
> - v1.3.11/v1.3.12 曾用 `device_class + suggested_unit_of_measurement` 方案，但 HA 会把 suggested_unit 固化成展示单位，导致小数值显示成 `0.00 MB/s`
> - v1.3.14 回退该方案，改为用户自选单位
> - v1.3.16 加修改确认步骤
> - v1.3.17 修复 translations 加载（自定义集成必须用 `translations/` 目录，不能用 `strings.json`）
> - **v1.4.0** 整合所有修复 + 在初次添加表单加单位修改提示语

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

| 传感器 | 说明 | 默认单位 | 可选单位 |
|--------|------|---------|---------|
| {设备名} Download Speed | 设备下载速率 | B/s | 同路由器速度单位 |
| {设备名} Upload Speed | 设备上传速率 | B/s | 同路由器速度单位 |
| {设备名} Download Total | 设备累计下载量 | B | 同路由器总量单位 |
| {设备名} Upload Total | 设备累计上传量 | B | 同路由器总量单位 |

**与 device_tracker 的区别**：
- `device_tracker` 的速率数据是属性（attribute），**不记录历史**，无法在图表中展示
- `device_sensor` 是独立传感器实体，**支持历史记录**，可直接用于 mini-graph-card 等图表卡片

**特性**：
- 新建条目时可选择设备，也可跳过稍后配置
- 已离线但之前选中的设备标注 `[离线]`，方便重新选中
- 取消勾选的设备，其传感器实体在重载后自动清理

### 🔐 加密算法自动检测

> 🆕 v1.3.5 新增，v1.3.8 重大修复

MiWiFi 路由器登录密码的哈希算法因固件版本不同而异：
- **新固件**（如 BE5000）：使用 SHA256+SHA256
- **老固件**（如 AX3600、AC2100、AX9000）：使用 SHA1+SHA1

本集成在登录前自动读取路由器 `init_info` 接口中的 `newEncryptMode` 字段判断加密方式，无需用户手动选择：
- `newEncryptMode=1` → 自动使用 SHA256
- 字段不存在或其他值 → 自动使用 SHA1
- 接口不可访问 → **默认使用 SHA1**（覆盖大多数老固件），登录失败自动切换到另一种算法

> **v1.3.8 修复内容**（基于用户日志反馈）：
> - 修复 `init_info` URL 路径只尝试 `/api/xqsystem/init_info` 的问题，新增尝试 `/cgi-bin/luci/api/xqsystem/init_info`（老固件路径）
> - 修复登录失败时只在错误消息含"密码错误"才切换算法的 BUG — 实际路由器返回英文"not auth"导致 SHA1 fallback 永不触发
> - 新增重试策略：每个算法尝试 1 次（共 2 次），不再依赖错误消息文本
> - 默认算法从 SHA256 改为 SHA1（覆盖更多老固件路由器）
> - 新增可选配置项 `force_hash_algo`，允许用户手动指定 SHA1/SHA256 跳过自动检测

> **v1.3.9 修复内容**（基于用户日志反馈）：
> - 修复快速连续登录时出现的 `code=1582 Invalid nonce` 错误
>   - 原因：`test_connection` 成功 logout 后，coordinator 几百毫秒内再次登录，两个 nonce 时间戳相同，被路由器拒绝
>   - 修复：nonce 使用真实客户端 MAC（`uuid.getnode()`），并增加单调计数器保证同一秒内 nonce 唯一
> - 识别 `code=1582 Invalid nonce` 错误码：不再误切换算法（这是 nonce 问题不是哈希问题），等待 2 秒后重试同算法，最多重试 2 次
> - 调试日志（`[DEBUG]` 前缀）从 `warning` 级别降回正常的 `debug` 级别，HA 默认配置下不再产生噪音。如需排查登录问题，在 `configuration.yaml` 中开启：
>   ```yaml
>   logger:
>     logs:
>       custom_components.miwifi_router: debug
>   ```

> **v1.3.10 修复内容**（v1.3.9 回归修复）：
> - 修复 v1.3.9 引入的 nonce 格式回归 BUG：v1.3.9 把 nonce 从 4 段改为 5 段（加了 counter），部分路由器严格校验 nonce 格式会拒绝 5 段 nonce，导致登录失败
> - 回到 4 段格式：`0_<MAC>_<时间戳>_<随机数>`，与 v1.3.6、dmamontov/hass-miwifi、路由器 JS 一致
> - 保留 v1.3.9 的真实 MAC 改进（用 `uuid.getnode()` 取本机网卡 MAC，比占位符更接近路由器 JS 的预期）
> - 保留 v1.3.9 的 `code=1582 Invalid nonce` 重试逻辑 — 仍然能处理同一秒内连续登录的极端情况
> - **如果你从 v1.3.6 升级到 v1.3.9 后登录失败，请升级到 v1.3.10**

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
| Xiaomi BE3600 | RD09 | 🔶 理论兼容 |
| Xiaomi AX9000 | RA69 | 🔶 理论兼容 |
| Xiaomi AX6000 | RA67 | 🔶 理论兼容 |
| Xiaomi AX3600 | RA70 | 🔶 理论兼容 |
| Xiaomi AX3000T | RB01 | 🔶 理论兼容 |
| Redmi AX6 | RA69 | 🔶 理论兼容 |
| Redmi AX5 | RB03 | 🔶 理论兼容 |
| Redmi AC2100 | RM2100 | 🔶 理论兼容 |

> - ✅ **已验证**：经实际设备测试确认可用
> - 🔶 **理论兼容**：基于相同 MiWiFi 固件 API 推断兼容，未经实际设备验证
>
> 其他运行 MiWiFi 固件的路由器理论上均兼容，欢迎反馈测试结果，验证后将升级为 ✅。

### 如何判断我的路由器是否支持？

只需在浏览器中直接访问以下两个地址之一（将 `192.168.31.1` 替换为你的路由器 IP）。**新固件和老固件使用不同的 URL 路径**，本集成会自动尝试这两个地址：

**地址 1（新固件，2023 年 5 月后机型）：**
```
http://192.168.31.1/api/xqsystem/init_info
```

**地址 2（老固件，AX3600/AC2100/AX9000 等）：**
```
http://192.168.31.1/cgi-bin/luci/api/xqsystem/init_info
```

判断方法：

- **任一地址显示 JSON 数据**，包含路由器型号、固件版本等信息，说明完全支持：

```json
// 新固件示例 (BE5000)
{"hardware":{"platform":"RD18","version":"1.0.53","displayName":"Xiaomi BE5000"}, "newEncryptMode":1, ...}

// 老固件示例 (AX3600, 没有 newEncryptMode 字段或字段为 0)
{"romversion":"1.0.168","countrycode":"CN","code":0,"model":"xiaomi.router.ra70","hardware":"RA70",...}
```

- **如果两个地址都返回 404 或 nginx 默认错误页**，说明路由器固件版本过老或固件分支不同，本集成可能无法使用

- **如何确认你的路由器用哪种加密算法**：看返回 JSON 里有没有 `newEncryptMode` 字段
  - `newEncryptMode=1` → 新固件，使用 SHA256（无需手动选，集成会自动检测）
  - `newEncryptMode` 字段缺失、为 0 或其他值 → 老固件，使用 SHA1（无需手动选，集成会自动检测）
  - 两个地址都打不开 → 集成默认用 SHA1，登录失败会自动切换到 SHA256 重试

> 💡 这些接口**都不需要登录**即可访问。集成会自动按"地址 1 → 地址 2"的顺序尝试，并用返回结果决定登录算法，所以你**不需要手动选择**算法 —— 默认的"Auto-detect"模式已经覆盖所有情况。
>
> 如果自动检测后登录仍然失败，可以在集成配置里手动选择 `Force SHA1` 或 `Force SHA256` 作为兜底。

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
   - **实时数据轮询间隔**（`scan_interval`）：默认 10 秒
   - **设备列表轮询间隔**（`device_scan_interval`）：默认 30 秒
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

### Q: 出现 invalid-auth 错误？

可能的原因和排查步骤：

1. **确认密码无误**：在浏览器中访问 `http://路由器IP` 并用相同密码登录，验证密码本身正确。

2. **v1.3.7 及更早版本的已知 BUG**：登录失败时只在错误消息含"密码错误"才切换 SHA1/SHA256 算法，但部分路由器返回英文"not auth"，导致 SHA1 fallback 永不触发。**请升级到 v1.3.8+**，新版重写了重试策略：每个算法尝试 2 次（共 4 次），不再依赖错误消息文本。

3. **手动指定算法**：升级到 v1.3.8+ 后，在配置流程的"密码哈希算法"选项中手动选择：
   - 老固件（AX3600、AC2100、AX9000、Redmi AX6/AX5、AX3000T 等）→ Force SHA1
   - 新固件（BE5000、BE3600、小米路由器 7000 等，2023 年 5 月后）→ Force SHA256

4. **检查 init_info 接口**：在浏览器中访问以下两个地址之一，查看返回的 `newEncryptMode` 字段：
   - `http://路由器IP/api/xqsystem/init_info` （新固件路径）
   - `http://路由器IP/cgi-bin/luci/api/xqsystem/init_info` （老固件路径）

   `newEncryptMode=1` 表示新固件 SHA256，缺失或其它值表示老固件 SHA1。

5. **反馈日志**：如果以上方法均无效，请在 [GitHub Issues](https://github.com/tiejiang29/miwifi_router/issues) 中提交 HA 日志（搜索 `[DEBUG]` 关键字），包含完整的登录过程日志。

### Q: 传感器显示"未知"？
检查路由器 IP 和密码是否正确，确保 HA 和路由器在同一局域网。可在配置中点击"验证"测试连接。

### Q: 实时数据轮询间隔和设备列表轮询间隔有什么区别？
- **实时数据轮询间隔**（`scan_interval`）：控制 WAN 上下行速率、累计流量、CPU 负载、内存使用率等路由器自身传感器的刷新频率。这些数据变化快，默认 10 秒即可获得平滑的速率曲线。
- **设备列表轮询间隔**（`device_scan_interval`）：控制设备在线状态、设备速率属性、设备传感器的刷新频率。设备列表 API 响应较重（包含所有连接设备的详细信息），默认 30 秒可以在及时检测上下线的同时减少对路由器的请求压力。

简单来说：想看更流畅的网速曲线就调小 `scan_interval`；想更快发现设备上下线就调小 `device_scan_interval`，但不要设得太小以免增加路由器负担。

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
├── api.py               # 路由器 API 客户端（登录、加密算法自动检测、stok缓存、Keep-Alive）
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
