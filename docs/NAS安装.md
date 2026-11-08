# NAS 安装与使用

本版在原项目基础上维护，保留上游许可证和插件。默认工作方式是：NAS 上运行订阅服务，定时将夸克分享转存到自己的夸克网盘。需要 NAS 本地文件时，可启用已有 Aria2 插件。

## 1. 用 Docker Compose 安装

支持安装了 Docker/Container Manager 的 Intel/AMD x86-64 和 ARM64 NAS。以下使用源码构建本维护版，不依赖原作者发布镜像，也不需要 Docker Hub 发布凭据。

1. 将你的维护仓库下载或克隆到 NAS，例如群晖 `/volume1/docker/quark-auto-save`、飞牛 `/vol1/1000/docker/quark-auto-save`。实际存储路径以你的 NAS 为准。
2. 将 `.env.example` 复制为 `.env`，把 `WEBUI_PASSWORD` 改成自己的管理密码。端口被占用时修改 `QAS_PORT`。
3. 在项目目录执行：

   ```sh
   docker compose up -d --build
   docker compose ps
   docker compose logs --tail=100 quark-auto-save
   ```

4. 浏览器访问 `http://NAS的局域网IP:5005`，使用 `.env` 中的账号密码登录。

Compose 会同时运行 `quark-auto-save` 和纯 API 版 PanSou。PanSou 只在容器网络中开放，服务地址是 `http://pansou:8888`，不占用 NAS 额外端口。首次新建配置时自动填入该地址。已有配置升级后，在“系统配置 → 资源搜索 → PanSou”中手动改为此地址并保存。

配置持久化在项目下 `config/`。必须挂载整个目录，目录需可写；请勿只把单个 JSON 文件 bind mount 到容器，本版用原子替换和旁边的 `.lock` 文件保护并发写入。

### 使用 NAS 图形管理器

- 群晖 DSM：Container Manager → 项目 → 新增，选择完整项目文件夹及 `compose.yaml`，准备好同目录 `.env` 后构建并启动。
- 飞牛 fnOS：Docker → Compose 项目，从完整项目目录创建；文件名和环境变量相同。
- 威联通：Container Station 创建 Compose 应用；若界面不支持 `build: .` 的完整构建上下文，可通过 SSH 在项目目录执行上述命令。

旧版只有 `docker-compose` 命令的系统可将命令中的 `docker compose` 替换为 `docker-compose`。使用前确认 NAS 已启用容器功能。

## 2. 第一次配置

1. 在自己的浏览器登录夸克网盘，按原项目方式取得 Cookie，粘贴到系统配置的第一个 Cookie 输入框并保存。不要将 Cookie、Aria2 密钥或真实 `config/` 上传 GitHub。
2. 确认 PanSou 启用，服务器是 `http://pansou:8888`。首次搜索可能需要等待索引源返回。
3. 设置“热门订阅的夸克网盘保存目录”，默认 `/追更`。
4. 默认每天 08:00、18:00、20:00 检查更新；可用 Crontab 调整。
5. 先在任务列表添加一个自己有权保存的分享，运行并检查日志与实际网盘目录。

新安装的任务列表为空，不会自动转存上游示例资源。原有 `config/quark_config.json` 的订阅不会被模板替换。

## 3. 搜索与自动换源

在任务名称中输入资源名，或点击名称旁的搜索按钮。PanSou 兼容新版原生接口和公共代理接口；支持提取码。搜索源超时或报错会明确显示，不会被伪装成“没有结果”。可填写备用 PanSou 地址，每行一个；主源失败或无结果时依次尝试。

“分享失效时自动搜索并替换”默认启用。每次正常任务运行时，会重新检查原分享和旧版标记失效的分享；只有明确取消、过期、不存在等失效响应才换源，登录、提取码、网络和限流错误会保留原订阅，下次重试。

候选需满足标题、季数和已知年份约束，并验证实际夸克分享内容。最多检查 5 个候选；无法可靠匹配时保留原订阅并输出日志。原保存目录、正则、重命名和周期保持不变。旧分享中的 `startfid` 无法迁移，会清除；最近 10 次替换记录保存在任务 `repair_history`，仅包含分享 ID，不包含验证 token。

开启“删除子目录后重存”的任务不会自动换源，以免新来源触发已有删除规则。单任务可在 JSON 配置中设置 `auto_repair: false` 禁用换源。

## 4. 热门追更

左侧“热门追更”按周一到周日显示，并分动漫、电视剧排序。一键订阅先搜索并验证资源，再保存任务并立即调用转存，之后按系统定时规则持续追更。重复点击或来源切换时会检查已有订阅。若浏览器在创建订阅后关闭，已保存的任务仍会在下一次定时运行时执行。

- 动漫首选 Bangumi 当季周播表，按“在看人数”排序。该表不保证反映临时停播、调档。
- 电视剧使用 TVmaze 中国播出日历，展示本周热度前 10 部，按实际集日期归日；不代表全网完整影视榜。
- Bangumi 不可达时，优先使用本周缓存，否则切换至 TVmaze 中国播出动画前 10。页面会说明覆盖范围和热度变化，不会伪造日番排期。
- 电视剧和备用动画会区分季数。来源未提供当前季年份或重拍版本时，仅同名同季仍可能无法完全区分版本；请核对实际目录。无法可靠确认的资源需要在任务列表手动选取。
- 数据缓存一小时，错误后五分钟重试。刷新按钮重新读取当前缓存，不会强制高频抓取外部源。
- 新热门订阅每日按定时规则检查；周历展示首播安排，网盘资源上传时间可能更晚，所以不会只在播出当天检查。

数据来源：[Bangumi API](https://github.com/bangumi/api)、[TVmaze API](https://www.tvmaze.com/api)。TVmaze 数据按 [CC BY-SA](https://creativecommons.org/licenses/by-sa/4.0/) 使用，页面保留条目来源链接。两种来源热度口径不同，不混合比较。

## 5. 下载到 NAS 本地磁盘

本项目已有 Aria2 下载插件，安装或使用 NAS 上现有的 Aria2 服务后：

1. 系统配置 → 插件 → aria2，填写 `host_port`（如 `http://NAS局域网IP:6800`）、`secret` 和 `dir`。
2. `dir` 是 **Aria2 进程看到的下载目录**。Aria2 在容器内运行时，需先将 NAS 下载文件夹挂载到该容器，例如 `/downloads`，这里填写 `/downloads`。
3. 开启“热门订阅同时通过 Aria2 下载到 NAS”并保存。
4. 此后新建的热门订阅会在转存成功后把新增文件提交给 Aria2，含子目录。已有订阅需要在任务插件选项中启用 `aria2.auto_download`、`download_subdir`。

这里不内置第二套下载器；Aria2 负责下载队列、磁盘空间和重试。夸克 Cookie、账号权限和下载直链可用性会影响实际下载。系统中“通知测试”按钮包含上游的转存/清理测试；建议验收自己的独立测试目录。

## 6. 更新、备份与回退

更新前备份整个 `config/` 目录。在项目目录：

```sh
docker compose stop quark-auto-save
cp -a config "config-backup-$(date +%Y%m%d-%H%M%S)"
git pull --ff-only
docker compose up -d --build
```

下载 ZIP 安装的用户可覆盖代码文件，再执行构建；保留 `.env` 和 `config/`。不要用上游 Watchtower 更新命令替换本维护版镜像。

回退时停止服务，检出上一个已验证提交或换回之前代码，再恢复对应配置备份、重新构建。备份里可能含 Cookie，妥善保管。

GitHub Actions 会先跑 Python 3.11/3.13 测试，再构建 amd64/arm64 镜像到该仓库的 GHCR。Fork 的 Actions 可能需要你先在 GitHub 的 Actions 页启用；仓库连接器还需要有该 Fork 的访问权限。镜像第一次发布后若为 private，需要在 Packages 中设为 public 或在 NAS 登录 GHCR；本地 `build` 安装不受此影响。只有 Actions 实际通过后才能确认远程镜像已发布。

## 7. 排错与验收

- 无法搜索：查看页面源错误和 `docker compose logs --tail=100 pansou`；确认地址为 `http://pansou:8888`。公共 `https://so.252035.xyz` 可作备用，但没有稳定性保证。
- 动漫源超时：页面会显示降级。如果你有自己的网络代理，可在 Compose 的服务 `environment` 中配置标准 `HTTPS_PROXY`，同时设置 `NO_PROXY: localhost,127.0.0.1,pansou`；不要关闭 TLS 校验。无需代理也能查看可达来源。
- “未找到匹配的有效资源”：日历有片名不代表网络上存在可用的夸克分享；在任务列表检查名称、季数、年份，手动选择正确分享。
- “配置已被更新”：任务自动换源或另一页面写入了新配置。刷新后重新编辑，避免把新订阅或修复结果覆盖掉。
- 订阅已创建但没转存：查看手动运行日志、Cookie 是否有效、目标网盘空间；在任务列表运行该任务重试。
- NAS 没有本地文件：先看网盘是否已转存，再检查 Aria2 的连接、密钥、容器目录映射和该任务 `auto_download` 选项。

建议验收顺序：登录 → 名称搜索 → 自有分享的单任务转存 → 热门周历切换 → 一键订阅 → 日志/网盘验证 → 按需验证 Aria2 本地文件。自动换源与并发保存已经有可重复模拟测试；真实账号操作需在你自己的环境完成。
