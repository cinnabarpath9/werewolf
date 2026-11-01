# 手机当主机（安卓 Termux 包）

这是「手机端部署」包：把安卓手机变成一台小服务器，同一 Wi-Fi 下的朋友用浏览器加入，各自坐一个座位。

> 快速路径与桌面端通用步骤见包根 `部署说明.md`（§8 是安卓速查）；**这份是手机上的完整手册**，
> 装不上的排障（依赖编译卡住、后台被杀）只在这里写全。
>
> 先说清楚：**iPhone / iPad 不能当主机**。iOS 不允许 Termux 这类能跑 Python 服务的环境，也没有可用的本地端口监听方案；iPhone 只能当**玩家**，用 Safari 打开别人主机的网址加入。想要「手机开房间」，目前现实可行的是安卓 + Termux。

## 你需要什么

- 安卓手机（建议 Android 9 以上，4GB 内存以上；6GB 更稳）
- Termux（**从 F-Droid 装**，不要用应用商店里那个多年没更新的旧版）
- 手机和朋友的设备在**同一个 Wi-Fi**（或者都连同一个 Tailscale 网络）
- 一条能用的模型 API Key（也可以先用「离线演练」不花钱试玩）

## 安装（一次性，约 3~5 分钟）

1. 装 Termux：打开 F-Droid，搜索 Termux，安装。
2. 把 `werewolf-android-1.0.0.zip` 传到手机，用文件管理器解压到 Termux 能访问的目录（例如 `Download/werewolf`）。
3. 打开 Termux，先让 Termux 能访问共享存储（只需一次）：

   ```bash
   termux-setup-storage
   ```

   手机弹出权限时点允许。

4. 进入目录并安装：

   ```bash
   cd ~/storage/downloads/werewolf
   bash ANDROID-INSTALL.sh
   ```

   第一次会下载 Python 依赖，慢一点正常；中途不要锁屏。

   如果卡在 `pydantic-core` / `aiohttp` 这类需要编译的包上（Termux 没有对应 wheel 时会发生），
   先补编译工具再重试：

   ```bash
   pkg install -y rust binutils libffi openssl
   python -m pip install --no-cache-dir -r requirements.txt
   ```

   安卓上首次安装可能要 10 分钟以上；装完一次之后启动只要几秒。

5. 启动：

   ```bash
   bash ANDROID-START.sh
   ```

   启动后窗口里会打印：

   - 手机自己打开：`http://127.0.0.1:8077`
   - 同一个 Wi-Fi 的朋友打开：`http://<手机IP>:8077`
   - 配对码（主机用）和联机码（朋友加入用）

## 开一局联机

1. 手机浏览器打开 `http://127.0.0.1:8077`，填一次**配对码**（写在 `配对码.txt`，启动日志里也有）。
2. 点首页的「联机房间 · 和朋友一起玩」。
3. 房间里能看到 **联机码**。把它或邀请链接发给朋友：
   `http://<手机IP>:8077/?join=<联机码>`
4. 朋友打开链接，填名字加入，点一个空座位「坐下」。
5. 主机选板子，点「开始游戏」。没坐人的座位由 AI 扮演；主机自己也可以不坐、当观众。

## 手机不熄屏 / 不掉线

- Termux 启动时会尝试申请唤醒锁（装了 `termux-api` 才生效）；没有也能跑。
- 到系统设置里把 Termux 的电池优化关掉（允许后台运行），否则锁屏后可能被系统杀掉。
- 最稳的做法：插着充电器、屏幕可以关，但别手动「强行停止」Termux。
- 服务被系统杀掉后，重新打开 Termux，再执行一次 `bash ANDROID-START.sh` 即可；存档还在。

## 朋友不在同一个 Wi-Fi

不要把 8077 端口直接映射到公网（没有 HTTPS、没有限流，等于把管理接口暴露出去）。推荐：

- 双方都装 **Tailscale**，登录同一个 tailnet；朋友用手机的 Tailscale IP 打开 `http://<tailscale-ip>:8077`。
- 或者让朋友用「电脑端包」跑在电脑上，手机只当玩家。

## iPhone 怎么加入

iPhone 不跑服务，只当玩家：

1. Safari 打开主机给的 `http://<主机IP>:8077/?join=<联机码>`。
2. 填名字、坐下。
3. 想装到主屏幕：Safari 分享 → 添加到主屏幕（需要 HTTPS 才能完整像 App；HTTP 也能玩，只是不能装 PWA）。

## 语音说明

- 文字局不需要麦克风，HTTP 页面就能完整玩。
- 手机浏览器只在 **HTTPS 或 localhost** 页面给麦克风权限；朋友用 `http://<IP>:8077` 打开时，「说」按钮会不可用。要语音就配 HTTPS（例如 Tailscale serve / 反向代理）。
- 主机的手机自己用 `http://127.0.0.1:8077` 打开属于 localhost，可以用麦克风。

## 数据与备份

- `.env`：API Key / 平台配置（第一次启动自动生成，也可以网页设置里填）。
- `settings.local.json`：网页「设置」里保存的模型、座位、语音配置。
- `games/`：对局存档（**删了只影响复盘**，不影响代码）。
- `var/voice_cache/`：语音缓存（可随时删，下次会重新合成）。
- `配对码.txt` / `联机码.txt`：两把不同的钥匙。

升级新版本时，**保留** `.env`、`settings.local.json`、`games/`，只替换代码，再执行一次 `bash ANDROID-INSTALL.sh`（依赖有更新时才需要重装）。

## 和电脑包的区别

两个包是**同一份源码**，区别只在启动方式：

| | 电脑包 | 安卓包 |
|---|---|---|
| 启动 | `start.bat` / `start.sh` | Termux + `ANDROID-START.sh` |
| 适合 | Windows / macOS / Linux 长期开 | 没有电脑、临时用手机开一局 |
| 稳定性 | 高（不会因锁屏被杀） | 看手机系统，建议插电 + 关电池优化 |
| 朋友加入 | 都支持 | 都支持 |

对局存档、`settings.local.json`、`.env` 不跨设备通用；换设备开新局即可。
