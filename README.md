# xiaoair

用小爱音箱当语音入口，让家里的 **AirPlay 音箱**播放 NAS 里的本地音乐。

> 对小爱说「小爱同学，影院播放晴天」——声音从你的好音箱出来，而不是小爱自己。

## 架构

```
你说话「小爱同学，影院晴天」
  │
  ▼
小爱音箱（只当麦克风，不出声）
  │  对话记录上小米云
  ▼
┌─ xiaoair（本项目）────────────────┐
│ 轮询小米云端对话记录(1s) → 解析指令    │
│ → 暂停小爱自身播放 + TTS 播报确认      │
└──────────────┬───────────────────────┘
               ▼ HTTP API
        OwnTone（音乐服务器，扫 NAS 曲库）
               ▼ AirPlay（RAOP，无损 ALAC）
        你的 AirPlay 音箱（出声）
```

## 特性

- 🎙️ **中文语音点歌**：播放/切歌/暂停/音量，触发词可自定义；点播单曲后自动将曲库其余歌曲随机加入播放列表
- 🔊 **AirPlay 输出**：经 OwnTone 推送无损 ALAC 到任意 AirPlay 音箱
- 📱 **Web 设置页**：小米登录、音箱选择、触发词配置全部可视化
- 🛡️ **小米风控验证内置**：新设备登录的短信验证直接在设置页完成（服务端对接小米 `identity` 接口，无需复制链接、无需过滑块）
- 🐳 **一键 Docker 部署**：与 OwnTone 联合 docker-compose，GitHub Actions 自动构建多架构镜像

## 快速开始

### 1. 部署

```bash
git clone https://github.com/cnstark/xiaoair.git
cd xiaoair
# 编辑 docker-compose.yml: 修改音乐目录路径、OT_URL 为主机 IP
docker compose up -d
```

> ⚠️ OwnTone 必须 `network_mode: host`，否则 mDNS 发现不了 AirPlay 音箱。
> xiaoair 不需要 host 网络。

### 2. 初始化

1. **OwnTone**（`http://<主机IP>:3689`，默认 admin/changeme）：等曲库扫描完成
2. **xiaoair 设置页**（`http://<主机IP>:8091`）：
   - 第 1 步：填小米账号密码 → 如果触发新设备风控，直接在页面里点「发送验证码」→ 输入短信验证码即可
   - 第 2 步：选择监听的小爱音箱
   - 第 3 步：选择输出的 AirPlay 音箱（OwnTone 发现的设备）
   - 第 4 步：触发词（默认「影院」）

### 3. 语音指令

| 指令（触发词=影院） | 作用 |
|---|---|
| 影院播放+歌名 / 影院+歌名 | 点歌（本地曲库搜索）；先播放指定歌曲，再随机播放曲库其余歌曲 |
| 影院播放+歌手+歌名 / 影院播放+歌名+歌手 | 指定歌手点歌，例如「影院播放周杰伦的晴天」 |
| 影院播放+歌手+的歌 / 影院播放+歌手+的歌曲 | 随机播放该歌手的一首歌，并将该歌手的全部歌曲随机加入播放列表 |
| 影院下一首 / 上一首 | 切歌 |
| 影院暂停 / 继续 / 停止 | 播放控制 |
| 影院音量调到 50 / 大一点 / 小一点 | 音量 |

只说歌名且曲库中有多个同名版本时，仍会选择曲库列表中先出现的版本；说出歌手可指定对应版本。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `/data` | 配置与登录态存储目录 |
| `OT_URL` | `http://192.168.1.2:3689` | OwnTone API 地址（改成你的主机 IP） |
| `OT_USER` / `OT_PASS` | `admin` / `changeme` | OwnTone 账号 |
| `TRIGGER` | `影院` | 语音触发词 |
| `PORT` | `8091` | 设置页端口 |

## 镜像

GitHub Actions 自动构建多架构镜像（linux/amd64, linux/arm64）：

```
ghcr.io/cnstark/xiaoair:latest
```

## 已知说明

- 指令含「播放」时会触发小爱原生音乐意图，小爱会先响一下再被桥接掐断（轮询架构固有 1~2s 延迟）。用不含「播放」的触发词可避免抢播。
- 对话记录轮询频率默认 1s，不建议再快（小米有限流）。
- 小米登录态保存在 `DATA_DIR`，重启容器不掉线；掉线后重新走一次页面登录即可。
- 已测试音箱入口型号：OH2P（XIAOMI 智能音箱 Pro），其他型号理论上兼容（同 xiaomusic 原理）。

## 致谢

- [xiaomusic](https://github.com/hanxi/xiaomusic) — 对话记录轮询原理
- [miservice-fork](https://pypi.org/project/miservice-fork/) — 小米云服务接口
- [OwnTone](https://github.com/owntone/owntone) — AirPlay 音乐服务器
- [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) — 小米风控验证流程参考

## 免责

仅供个人学习与非商业自用。小米账号密码与登录态仅保存在你自己的服务器本地。请确保你播放的音乐文件来源合法。
