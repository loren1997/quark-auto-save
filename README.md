> 本维护版新增：网盘索引与公开网页搜索、夸克链接提取、多语言片名匹配、失效订阅自动换源、独立国漫/动漫/电视剧周历和一键订阅，支持复用 Aria2 下载到 NAS。
>
> **V3.11：** 热门周历先显示可用快照，后台逐来源更新，慢接口和中文别名查询不再阻塞整页；同周来源失败时保留其上次成功数据并标注缓存，详细提示可展开查看。失效换源、补集和待查找订阅统一遵守任务运行星期及截止日期；手动点击单个任务仍立即运行。每个运行日使用已有系统定时规则，不额外创建转存计划。
>
> **V3.10：** 新增 PansoVIP 公开站内搜索，电视剧周历从每周最多 10 部扩展为 50 部，按真实播出日期展示并按需补齐 TVmaze 官方别名。新增默认开启的后台渠道巡检和公开频道自动发现：固定上游目录每天更新，有限量验证后接入；故障来源冷却到期后复查，用户自定义频道保留。可在“系统配置 → 资源搜索”关闭这两个功能或查看渠道状态。遇到验证码会继续使用其他来源，不要求人工处理；不保证所有网站免费、稳定可用，也不保证缺集必然补齐。详情见 [NAS 安装与使用](docs/NAS安装.md) 和 [验证记录](docs/验证记录.md)。
>
> **V3.9：** 修复慢搜索源耗尽预算后丢掉已返回链接、没有时间读取资源帖的问题；识别简繁中文、Baha 等发行标签与同作品字幕组目录。Meijusou 主文章内的分享链接与完整片名绑定，排除推荐区。首季 Bangumi 动画仅在资源帖、真实目录、全部视频完整片名及集号一致且无已知年季冲突时接受缺省年份，日志明确标注“年份未确认”。这些证据随资源历史保留，删除任务再订阅时仍会重新验证。网站验证码源会跳过，不要求用户完成验证码；其他来源和定时重试继续，但不能保证外部资源始终可用。
>
> **V3.8.2：** 识别公共 PanSou 的特定 HTTP/HTTPS 上游协议错误，短期冷却后自动重试，避免后续订阅反复等待该故障源。本地 PanSou 新增两个无需账号的插件，共 7 个。移除已失去动漫内容的旧默认 Telegram 频道，仅迁移原厂名单，自定义频道保留。只读网络检查会标明 HTTP 200 浏览器验证页，避免把状态码成功当成搜索可用。
>
> **V3.8.1：** 订阅只读取目标作品的日历来源，减少无关 API 请求；Bangumi 别名遇到暂态网络错误会在时间上限内重试一次。搜索来源超时、403、429 或验证码按实际域名跨任务冷却，尊重 `Retry-After`，继续查询其他来源。Compose 中 PanSou 与应用共同使用可选 `QAS_PROXY`；新增只读网络检查脚本。更新不会解除外站防护，也不会放开私网地址访问限制。
>
> **V3.8：** 支持多部作品依次排队订阅，刷新页面及服务重启后保留未执行队列；首次未找到资源的订阅会保留并按现有定时规则自动重试。新增 Telegram 公开消息与腾讯频道公开帖子索引，独立配置频道名单。修复 Meijusou 正常空结果被误判故障、有效别名被跳过，以及季度合集内作品目录验证失败。网站防护、限流或连接失败仍会如实报告，不能保证所有作品都有可用分享。
>
> **V3.7：** 增加 Meijusou 公开站内搜索与搜狗网页搜索，补集支持字幕组子目录和带发行标签的视频文件名。任务与一键订阅改为静默后台运行，新增独立“运行日志”页，显示当前作品、来源、查询/文件和步骤；关闭或刷新页面不影响运行。实际下载进度仍在 Aria2 查看。
>
> **V3.6：** 已找到的资源线索独立保存在 `config/quark_config.resources.json`，仅保存标题、别名和分享链接；删除任务后再次搜索仍会尝试这些链接，每次都重新验证。NAS 更新命令会从当前配置与旧备份导入线索，不恢复已删除的任务。网页搜索扩展为 Google、DuckDuckGo 和 Bing。
>
> 补集检查分享内的实际视频，不要求资源标题标注集数或“全集”；每轮最多使用 3 份候选分享，按剩余缺号继续补入并交给下载插件。未补齐时下次运行继续查找；备用分享文件改名失败也会保留记录并重试。
>
> **V3.5：** 修复缺集搜索未带集号、故障来源反复等待、超时丢弃已返回候选及视频分类误拒绝。补集会尝试集号、中文别名和整季分享，并在失败日志中列出实际查询词；仍须验证同作品、同季、已知年份和真实缺集视频。
>
> 热门订阅默认将视频、字幕平铺转存到夸克 `/cartoon`，按 `片名 S01E01.原后缀` 命名。开启 Aria2 后默认只下载视频到 `/downloads/cartoon`，不复制来源或剧名子目录；字幕下载需另行开启。确认分享无视频时，跳过并查找经过验证的新来源。
>
> 旧工厂默认目录和命名规则会迁移，自定义设置保留；更新不移动或删除已有 NAS 文件。中间缺集会尝试补搜，但不能保证找到有效资源。
>
> **安装本维护版请使用 [NAS 安装说明](docs/NAS安装.md)。** 与原版并行运行请选发布页的 NAS 安装包，默认端口 `5006`。下文 `cp0204/quark-auto-save` 镜像及原作者更新链接属于上游版本，不包含这些改动。
>
> 开发者验证：`python -m unittest discover -s tests -v`；可选无头浏览器检查：`python tests/ui_smoke.py`（需安装 Playwright 和 Chromium）。

<div align="center">

![quark-logo](img/icon.png)

# 夸克网盘自动转存

夸克网盘签到、自动转存、命名整理、发推送提醒和刷新媒体库一条龙。

对于一些持续更新的资源，隔段时间去转存十分麻烦。

定期执行本脚本自动转存、文件名整理，配合 [SmartStrm](https://github.com/Cp0204/SmartStrm) / [OpenList](https://github.com/OpenListTeam/OpenList) , Emby 可达到自动追更的效果。🥳


[![zread](https://img.shields.io/badge/Ask_AI-_.svg?style=flat&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff)](https://zread.ai/Cp0204/quark-auto-save) [![skill](https://img.shields.io/badge/🦞-ClawHub%20Skill-orange?logo=openclaw)](https://clawhub.ai/Cp0204/quark-auto-save)

[![wiki][wiki-image]][wiki-url] [![github releases][gitHub-releases-image]][github-url] [![docker pulls][docker-pulls-image]][docker-url] [![docker image size][docker-image-size-image]][docker-url]

[wiki-image]: https://img.shields.io/badge/wiki-Documents-green?logo=github
[gitHub-releases-image]: https://img.shields.io/github/v/release/Cp0204/quark-auto-save?logo=github
[docker-pulls-image]: https://img.shields.io/docker/pulls/cp0204/quark-auto-save?logo=docker&&logoColor=white
[docker-image-size-image]: https://img.shields.io/docker/image-size/cp0204/quark-auto-save?logo=docker&&logoColor=white
[github-url]: https://github.com/Cp0204/quark-auto-save
[docker-url]: https://hub.docker.com/r/cp0204/quark-auto-save
[wiki-url]: https://github.com/Cp0204/quark-auto-save/wiki

![run_log](img/run_log.png)

</div>

> [!CAUTION]
> ⛔️⛔️⛔️ 注意！资源不会每时每刻更新，**严禁设定过高的定时运行频率！** 以免账号风控和给夸克服务器造成不必要的压力。雪山崩塌，每一片雪花都有责任！

> [!NOTE]
> 开发者≠客服，开源免费≠帮你解决使用问题；本项目 Wiki 已经相对完善，遇到问题请先翻阅 Issues 和 Wiki ，请勿盲目发问。

## 功能

- 部署方式
  - [x] 可能~~兼容青龙~~
  - [x] Docker 部署，WebUI 配置

- 分享链接
  - [x] 支持分享链接的子目录
  - [x] 记录失效分享并跳过任务
  - [x] 支持需提取码的分享链接 <sup>[?](https://github.com/Cp0204/quark-auto-save/wiki/使用技巧集锦#支持需提取码的分享链接)</sup>
  - [x] 智能搜索资源并自动填充 <sup>[?](https://github.com/Cp0204/quark-auto-save/wiki/CloudSaver搜索源)</sup>

- 文件管理
  - [x] 目标目录不存在时自动新建
  - [x] 跳过已转存过的文件
  - [x] 正则过滤要转存的文件名
  - [x] 转存后文件名整理（正则替换）
  - [x] 可选忽略文件后缀

- 任务管理
  - [x] 支持多组任务
  - [x] 任务结束期限，期限后不执行此任务
  - [x] 可单独指定子任务星期几执行

- 媒体库整合
  - [x] 根据任务名搜索 Emby 媒体库
  - [x] 追更或整理后自动刷新 Emby 媒体库
  - [x] 插件模块化，允许自行开发和挂载[插件](./plugins)

- 其它
  - [x] 每日签到领空间 <sup>[?](https://github.com/Cp0204/quark-auto-save/wiki/使用技巧集锦#每日签到领空间)</sup>
  - [x] 支持多个通知推送渠道 <sup>[?](https://github.com/Cp0204/quark-auto-save/wiki/通知推送服务配置)</sup>
  - [x] 支持多账号（多账号签到，仅首账号转存）

## 部署

### Docker 部署

Docker 部署提供 WebUI 进行管理配置，部署命令：

```shell
docker run -d \
  --name quark-auto-save \
  -p 5005:5005 \ # 映射端口，:前的可以改，即部署后访问的端口，:后的不可改
  -e WEBUI_USERNAME=admin \
  -e WEBUI_PASSWORD=admin123 \
  -v ./quark-auto-save/config:/app/config \ # 必须，配置持久化
  -v ./quark-auto-save/media:/media \ # 可选，模块alist_strm_gen生成strm使用
  --network bridge \
  --restart unless-stopped \
  cp0204/quark-auto-save:latest
  # registry.cn-shenzhen.aliyuncs.com/cp0204/quark-auto-save:latest # 国内镜像地址
```

docker-compose.yml

```yaml
name: quark-auto-save
services:
  quark-auto-save:
    image: cp0204/quark-auto-save:latest
    container_name: quark-auto-save
    network_mode: bridge
    ports:
      - 5005:5005
    restart: unless-stopped
    environment:
      WEBUI_USERNAME: "admin"
      WEBUI_PASSWORD: "admin123"
    volumes:
      - ./quark-auto-save/config:/app/config
      - ./quark-auto-save/media:/media
```

管理地址：http://yourhost:5005

| 环境变量         | 默认       | 备注                                     |
| ---------------- | ---------- | ---------------------------------------- |
| `WEBUI_USERNAME` | `admin`    | 管理账号                                 |
| `WEBUI_PASSWORD` | `admin123` | 管理密码                                 |
| `PORT`           | `5005`     | 管理后台端口                             |
| `PLUGIN_FLAGS`   |            | 插件标志，如 `-emby,-aria2` 禁用某些插件 |
| `TASK_TIMEOUT`   | `1800`     | 任务执行超时时间（秒），超时则任务结束   |

#### 一键更新

```shell
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock containrrr/watchtower -cR quark-auto-save
```

<details open>
<summary>WebUI 预览</summary>

![screenshot_webui](img/screenshot_webui-1.png)

![screenshot_webui](img/screenshot_webui-2.png)

</details>

## 使用说明

### 正则处理示例

| pattern                                | replace                 | 效果                                                                   |
| -------------------------------------- | ----------------------- | ---------------------------------------------------------------------- |
| `.*`                                   |                         | 无脑转存所有文件，不整理                                               |
| `\.mp4$`                               |                         | 转存所有 `.mp4` 后缀的文件                                             |
| `^【电影TT】花好月圆(\d+)\.(mp4\|mkv)` | `\1.\2`                 | 【电影TT】花好月圆01.mp4 → 01.mp4<br>【电影TT】花好月圆02.mkv → 02.mkv |
| `^(\d+)\.mp4`                          | `S02E\1.mp4`            | 01.mp4 → S02E01.mp4<br>02.mp4 → S02E02.mp4                             |
| `$TV`                                  |                         | [魔法匹配](#魔法匹配)剧集文件                                          |
| `^(\d+)\.mp4`                          | `{TASKNAME}.S02E\1.mp4` | 01.mp4 → 任务名.S02E01.mp4                                             |

更多正则使用说明：[正则处理教程](https://github.com/Cp0204/quark-auto-save/wiki/正则处理教程)

> [!TIP]
>
> **魔法匹配和魔法变量**：在正则处理中，我们定义了一些“魔法匹配”模式，如果 表达式 的值以 $ 开头且 替换式 留空，程序将自动使用预设的正则表达式进行匹配和替换。
>
> 自 v0.6.0 开始，支持更多以 {} 包裹的我称之为“魔法变量”，可以更灵活地进行重命名。
>
> 更多说明请看[魔法匹配和魔法变量](https://github.com/Cp0204/quark-auto-save/wiki/魔法匹配和魔法变量)

### 刷新媒体库

在有新转存时，可触发完成相应功能，如自动刷新媒体库、生成 .strm 文件等。配置指南：[插件配置](https://github.com/Cp0204/quark-auto-save/wiki/插件配置)

媒体库模块以插件的方式的集成，如果你有兴趣请参考[插件开发指南](https://github.com/Cp0204/quark-auto-save/tree/main/plugins)。

### 更多使用技巧

请参考 Wiki ：[使用技巧集锦](https://github.com/Cp0204/quark-auto-save/wiki/使用技巧集锦)

## 生态项目

以下展示 QAS 生态项目，包括官方项目和第三方项目。

### 官方项目

* [QAS一键推送助手](https://greasyfork.org/zh-CN/scripts/533201-qas一键推送助手)

  油猴脚本，在夸克网盘分享页面添加推送到 QAS 的按钮

* [SmartStrm](https://github.com/Cp0204/SmartStrm)

  STRM 文件生成工具，用于转存后处理，媒体免下载入库播放。

### 第三方开源项目

> [!TIP]
>
> 以下第三方开源项目均由社区开发并保持开源，与 QAS 作者无直接关联。在部署到生产环境前，请自行评估相关风险。
>
> 如果您有新的项目没有在此列出，可以通过 Issues 提交。

* [nonebot-plugin-quark-autosave](https://github.com/fllesser/nonebot-plugin-quark-autosave)

  QAS Telegram 机器人，快速管理自动转存任务

* [Astrbot_plugin_quarksave](https://github.com/lm379/astrbot_plugin_quarksave)

  AstrBot 插件，调用 quark_auto_save 实现自动转存资源到夸克网盘

* [Telegram 媒体资源管理机器人](https://github.com/2beetle/tgbot)

  一个功能丰富的 Telegram 机器人，专注于媒体资源管理、Emby 集成、自动下载和夸克网盘资源管理。

## 打赏

如果这个项目让你受益，你可以无偿赠与我1块钱，让我知道开源有价值。谢谢！

![WeChatPay](https://cdn.jsdelivr.net/gh/Cp0204/Cp0204@main/img/wechat_pay_qrcode.png)

## 声明

本项目为个人兴趣开发，旨在通过程序自动化提高网盘使用效率。

程序没有任何破解行为，只是对于夸克已有的API进行封装，所有数据来自于夸克官方API；本人不对网盘内容负责、不对夸克官方API未来可能的变动导致的影响负责，请自行斟酌使用。

开源仅供学习与交流使用，未盈利也未授权商业使用，严禁用于非法用途。

## LICENSE

本项目基于 [AGPL-3.0](LICENSE)  协议开源，这意味着：

1. **传染性与开源义务**：任何使用了本项目代码的作品（包括修改或链接）在分发时，必须以 AGPL-3.0 开源。
2. **网络服务也需开源**：基于该项目衍生的网络服务（如 API、Web 等）需要向用户提供修改后源代码的获取方式。
3. **修改与分发要求**：必须保留原版权声明和许可证、修改后的文件需标注修改说明、分发时必须提供完整的源代码。

## Sponsor

CDN acceleration and security protection for this project are sponsored by Tencent EdgeOne.

<a href="https://edgeone.ai/?from=github" target="_blank"><img title="Best Asian CDN, Edge, and Secure Solutions - Tencent EdgeOne" src="https://edgeone.ai/media/34fe3a45-492d-4ea4-ae5d-ea1087ca7b4b.png" width="300"></a>
