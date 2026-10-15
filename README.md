# Firefly-Table-Pet-Online-Server · 流萤桌宠服务端

流萤桌宠（在线版）服务端，当前版本 **v4.7.0**（Windows 10/11 64 位）。

客户端仓库：<https://github.com/Jimhow-Gu/Firefly-Table-Pet-Online>

---

## 一、快速开始

1. 解压 `流萤桌宠服务端_v4.7.0.zip` → 双击 `流萤桌宠服务端/流萤桌宠服务端.exe`
2. 在窗口里点 **「启动服务器」**（不会自动开始监听）
3. 首次运行会在 exe 同目录自动创建 `server_data/`（玩家存档、配置、对外资源）
4. 玩家在客户端「🌐 多人联机 → 第三方的服务器功能」里填 `你的IP:端口` 即可进入

> ⚠️ 端口要和客户端填的一致。默认 **20082**，可以在窗口左下角直接改（见下）。

## 二、窗口说明

```
┌──────────────────────────────────────────────────────────┐
│  服务器日志（右键：复制 / 全选）                          │
│                                                          │
├──────────────────────────────────────────────────────────┤
│ 服务器时区: [________▼]  端口: [____]  文本大小: [__]  [应用并重启] │
│ （命令输入框，/help 看用法）                      [发送]   │
│ [📝 管理内容] [启动服务器] [重启服务器] [停止服务器] [检查更新] │
└──────────────────────────────────────────────────────────┘
```

- **服务器时区**：下拉选择。为避免误触，**鼠标滚轮划过不会改时区**（以前会）。
- **端口**：TCP 监听端口；**默认/留空 = 20082**，可填 1~65535 的整数。
  改端口后客户端地址要同步改成 `IP:新端口`（窗口日志会提示）。
- **文本大小**：日志/控制台字号（整数 px，6~72），默认 13；字体族与服务端保持一致（Consolas / 微软雅黑）。
- **应用并重启**：上面三项**共用一个按钮**（在最右侧），一次把时区 + 端口 + 文本大小都保存；
  服务器正在运行时会提示并重启（在线玩家会断开），未运行时保存即可、下次启动生效。
- **右键菜单**：日志区与命令行输入框用的是自己写的菜单，只有 **复制 / 全选**（没选中内容时「复制」为灰色）。
- 开关服务器也可以用 **启动 / 重启 / 停止服务器** 按钮；**检查更新** 用来拉取最新资源。

## 三、端口

| 端口 | 用途 | 在哪里改 |
| --- | --- | --- |
| **20082**（默认，可自定义） | TCP 游戏/聊天协议，客户端连这个 | 窗口设置行的「端口」（留空=20082），存 `server_data/server_config.json` 的 `tcp_port` |
| **8080** | HTTP：管理页 + 资源下载（自定义物品图片、卡池页面、加密资源包） | `server_config.json` 的 `web_port`；8080 被占用时会自动往后找 8080~8099 |

## 四、管理页（📝 管理内容）

- **菜单 / 物品 / 商店 / 卡池** 全部可视化编辑，改完点 **🔥 热更新** 立即广播给在线客户端，或 **📦 发布** 重新打包资源
- **物品必须上传图片**：背包和商店都直接使用这张图（上传后「待上传」会在保存成功时自动变成「当前：…」；
  点「热更新 / 发布」时若有还没保存的图片，会先替你保存一次）
- **卡池**：卡池有效期（= 卡池剩余时间，抽卡页显示到秒）/ 按钮有效期、保底、UP、封面与展示图、上架公开
- **商店**：上架/下架、价格与货币、排序
- 兑换：**1600 星琼 = 10 张星铁专票**（在卡池页兑换）

## 五、账号与连接安全

- **不存明文口令**：`PBKDF2-HMAC-SHA256(口令, 盐, 120000)` 派生 `StoredKey`，登录只发 `HMAC(StoredKey, nonce)`
  （SCRAM 风格挑战应答），服务端只存盐与 StoredKey
- **三态账号 + 灰度强制**：`security_level` 四档 —— `0` 关 / `1` 只告警（影子）/ `2` 未绑定→受限会话 / `3` 未绑定→拒绝登录
- **TOFU / 绑定码**：首次登录与设备建立信任；管理员可用 `/bindcode` 发新绑定码、`/unbind` 解绑
- **防枚举**：未知账号返回确定性假盐；失败登录按 IP 限速；多端登录与失败会告警
- **审计**：`server_data/login_audit.jsonl`（窗口命令 `/audit` 可直接看最近记录、`/unbound` 看未绑定账号）
- **网关部署**（推荐）：把服务端放在 mTLS/VPN 网关后面，`server_config.json` 里
  `listen_host` 设为内网地址（如 `127.0.0.1`）、`allow_hosts` 填网关来源白名单 —— 后端只信任网关

## 六、资源包安全

- 资源包加密：magic `FLYENC1`，密钥 = `sha256(盐 + pack_secret + menu_id)`，与数据做流密钥异或
- 每个资源包附带 **manifest 签名**（逐文件 sha256 + HMAC），客户端校验通过才落盘
- `pack_secret` 在 `server_data/server_config.json` 里，**换掉它等于让所有客户端重新获取资源**（可用于强制刷新）

## 七、控制台命令（窗口输入框，`/help` 看全部）

| 分类 | 命令 |
| --- | --- |
| 玩家 | `/give` `/set` `/clear` `/kick` `/ban` `/unban` `/blacklist` `/whitelist` `/op` `/deop` `/resetname` |
| 安全 | `/bindcode` `/unbind` `/passwd` `/seclevel` `/audit` `/unbound` |
| 服务器 | `/maxplayers` `/access` `/mute` `/recall` `/redeemstats` `/function` `/restart` `/stop` `/help` |

## 八、目录结构

```
流萤桌宠服务端.exe
server_data/
├── players/            玩家存档（JSON）
├── webroot/            对外资源：custom_items/（管理员上传的物品图）、
│                       gacha_images/（卡池图）、resources/（页面与加密资源包）
├── web_resources/      图标等静态资源
├── server_config.json  服务器名、web_port、tcp_port、log_font_size、security_level、listen_host、allow_hosts…
├── shop_config.json    商店商品
├── gacha_config.json   卡池配置（卡池有效期等）
├── custom_items.json   自定义物品（含图片路径与版本号 ver）
├── custom_menu.json    自定义菜单
├── redeem_codes.json   兑换码
└── login_audit.jsonl   登录审计
html/index.html         管理页（打进 exe）
gacha_data/index.html   卡池页模板（打进 exe）
font/  images/          字体与图片素材（打进 exe）
```

## 九、从源码运行 / 自行打包

需要 Python 3.10：

```
PyQt5==5.15.2      # 含 QtWebEngineWidgets / QtWebChannel / QtWebEngineCore
pytz
```

```bash
python main.py     # 源码运行
```

自行打包（PyInstaller onefile，`html` / `font` / `images` 要一起打进去）：

```bash
pyinstaller --noconfirm --clean --onefile --windowed --name FireflyServer ^
  --add-data "html;html" --add-data "font;font" --add-data "images;images" ^
  --hidden-import pytz main.py
```

## 十、注意

- 服务端**不会自动开始监听**，必须点「启动服务器」（或重启）后才接受连接。
- 公网部署请自行做好端口转发/防火墙，并强烈建议按第五节的网关方式收紧监听地址。
- 最近一轮改动：窗口新增可自定义端口（默认/留空 20082）、文本大小、共用的「应用并重启」按钮、
  时区禁止滚轮误改、日志/命令行自定义右键菜单（全选/复制）；管理页修复「待上传」不消失与发布前不落盘的问题。

## 十一、声明

仅供学习与交流使用，请勿用于商业用途；《崩坏：星穹铁道》相关素材与角色版权归米哈游所有。
