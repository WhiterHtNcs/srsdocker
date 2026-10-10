# singbox-srs-generator

一个使用 Python 实现的 sing-box 规则集生成工具，支持生成 **sing-box SRS 二进制规则集** 和 **完整 OpenClash 配置文件**。

## 功能

- **规则集管理**：新建、编辑、删除、查看、手动排序 `mapping/rules/*.txt`
- **规则转换**：生成 sing-box JSON 规则文件
- **SRS 生成**：调用 `sing-box rule-set compile` 生成 `.srs`
- **OpenClash 完整配置生成**：
  - 从 `mapping/config/subscribe.json` 读取多机场订阅，生成 `proxy-providers`
  - 从 `mapping/config/template.yaml` 读取策略组结构，自动填充提供商列表
  - 可按机场启用 DNS 下发，从完整订阅配置提取 DNS，仅适配对应节点服务器域名
  - 从 `mapping/rules/*.txt` 读取规则，内联到 `rules:` 节（无需外部文件引用）
  - IP 规则自动合并（`Direct.txt` + `DirectIP.txt` → `Direct`）
  - 每条规则类别自动生成独立 select 策略组，可手动选节点
- **远程规则同步**：按规则集中使用到的 `geosite:` / `geoip:` 下载对应 JSON
- GitHub token 支持：通过前端配置或 Docker 环境变量 `GITHUB_TOKEN` 配置
- Docker 部署：内置端口 `9044`
- DNS 下发使用 PyYAML 解析订阅配置，Docker 构建时自动安装

## 目录结构

```text
.
|-- app.py                 # 后端 HTTP 服务
|-- requirements.txt       # Python 依赖（PyYAML）
|-- Dockerfile
|-- docker-compose.yml
|-- docker/
|   `-- entrypoint.sh      # 容器启动与 cron 初始化
|-- mapping/               # 运行时数据（gitignored）
|   |-- bin/               # sing-box 二进制文件
|   |-- config/            # 订阅、模板、country_filter.json、residential_filter.json 等运行时配置
|   |   |-- providers/     # 下载的机场完整订阅配置，命名为 机场名.yaml
|   |-- rules/             # 用户规则 txt
|   |-- rules-dat/         # 下载的 geosite / geoip JSON
|   `-- rule-set/
|       |-- srs/           # 生成的 SRS 文件
|       `-- openclash/
|           `-- openclash.yaml # 完整配置（可直接上传 OpenClash）
|-- web/
|   `-- index.html         # 前端页面
`-- .gitignore
```

## 快速开始

### Docker Compose

```bash
# 1. 准备运行时目录及私人配置（不会提交 Git）
mkdir -p mapping/config mapping/rules mapping/bin
vim mapping/config/subscribe.json    # 填入真实的订阅 URL

# 2. 编辑 OpenClash 模板（按需调整策略组和规则映射）
vim mapping/config/template.yaml

# 3. 启动服务
docker compose up -d

# 4. 访问 Web 界面
#    http://localhost:9044
```

Compose 设置 `pull_policy: build`，直接从当前源码构建，产物名称为 `srsdocker:latest`。构建设置 `pull: false`，优先复用已有基础镜像；首次构建仍需下载缺少的基础镜像和安装依赖。

### 更新已有 Docker 容器

在部署目录执行。普通的后端、前端或生成器更新无需重新构建镜像：

```bash
cd /你的/srsdocker/部署目录
git pull --ff-only
docker compose restart
```

若本次更新包含 `Dockerfile`、`docker-compose.yml` 或容器依赖变更，则改用重新构建：

```bash
cd /你的/srsdocker/部署目录
git pull --ff-only
docker compose up -d --build
```

`mapping/` 是挂载的运行时数据目录，执行上述更新不会覆盖其中的订阅、模板、规则和已生成配置。更新后刷新网页；若修改了生成逻辑或模板，再点击“生成 OpenClash”生成新的配置文件。

### 本地运行

```bash
python -m pip install -r requirements.txt
python app.py
```

## 配置说明

### `mapping/config/subscribe.json`

记录机场订阅信息，**私人配置，不提交 Git**：

```json
{
  "user_agent": [
    "clash-verge/v2.2.3",
    "ClashMetaForAndroid/2.11.2.Meta"
  ],
  "providers": [
    {
      "name": "YToo_Trojan",
      "url": "https://your-subscribe-url",
      "interval": 86400,
      "use_for_ai": true,
      "use_for_residential": false,
      "use_for_dns": false,
      "use_for_latency": false,
      "override": {
        "additional-prefix": "Main："
      }
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `user_agent` | 全局 UA（字符串或数组），各 provider 可单独覆盖 |
| `providers[].name` | 提供商名称 |
| `providers[].url` | 订阅地址 |
| `providers[].interval` | 更新间隔（秒，默认 86400） |
| `providers[].health_check` | 可选，健康检查配置 |
| `providers[].use_for_ai` | 是否为 AI 策略组生成“机场·国家”测速组，默认 `true` |
| `providers[].use_for_residential` | 是否创建该机场的家宽手动选择组，独立于 AI 开关，默认 `false` |
| `providers[].use_for_dns` | 是否从订阅提取 DNS 并为该机场节点服务器域名生成定向解析策略，默认 `false` |
| `providers[].use_for_latency` | 是否让该机场组自动选择延迟最低节点，默认 `false` |
| `providers[].override.additional-prefix` | 节点名前缀 |

### `mapping/config/config.json`

除远程规则源与定时任务外，此文件还保存全部测速组共用的参数：

```json
{
  "url_test": {
    "url": "https://www.gstatic.com/generate_204",
    "interval": 300,
    "tolerance": 50,
    "timeout": 5000,
    "lazy": true
  }
}
```

- `interval`：测速间隔，单位秒。
- `tolerance`：切换容差，单位毫秒。
- `timeout`：单次测速超时，单位毫秒。
- `lazy`：是否按需测速。

可在 Web UI 的“测速设置”标签页修改；保存后需重新生成 OpenClash 配置才会写入 YAML。

### `mapping/config/template.yaml`

OpenClash 配置模板，**私人配置，不提交 Git**。包含：
- 基础设置（端口、DNS、Sniffer 等）
- 策略组结构（proxy-groups）
- 规则映射（rule_mapping）
- 自定义规则（custom_rules）

模板中使用占位符自动替换：

| 占位符 | 替换为 |
|--------|--------|
| `__ALLNODES__` | 全部节点（机场名列表，放 `proxies:` 引用机场组，放 `use:` 平铺所有节点） |
| `__PROVIDER_GROUPS__` | 各机场策略组定义；`use_for_latency: true` 时为 url-test，否则为 select |
| `__PROVIDER_COUNTRY_GROUPS__` | 所有启用 AI 的“机场·国家”延迟测速组定义，国家及过滤规则由 country_filter.json 决定 |
| `__PROVIDER_COUNTRY_NODES__` | 所有启用 AI 的“机场·国家”组名称列表，可嵌入 AI 的 `proxies:` |
| `__PROVIDER_RESIDENTIAL_GROUPS__` | 每个勾选家宽的机场一个独立“机场·家宽”手动选择组定义；无人勾选时为空 |
| `__RESIDENTIAL_NODE__` | 所有勾选家宽的“机场·家宽”组名称列表，可嵌入 AI 或其他策略组的 `proxies:`；无人勾选时为空 |
| `__RULE_GROUPS__` | 各规则类别对应的 select 策略组（rule_mapping 中值=键的条目） |
| `__PROXY_PROVIDERS__` | proxy-providers 插入位置 |
| `__URL_TEST_OPTIONS__` | 统一的 url-test 参数（url、interval、tolerance、timeout、lazy） |

订阅页面的家宽策略默认不勾选，独立于 AI 开关。每个勾选的机场只生成一个独立的“机场·家宽”手动选择组，AI 直接引用各机场家宽组；无人勾选时不创建机场家宽组。每个机场家宽组仅引用对应机场的订阅，筛选名称同时包含家宽关键词及任一已启用国家标识的节点，所有已启用国家的家宽节点合并在同一个组中。有已启用国家且勾选后即创建组，不预先检查订阅中是否有匹配节点。

AI 国家组和家宽组共用 `mapping/config/country_filter.json`，默认包含美国、新加坡、台湾、日本，保留原有中文、英文、地区缩写及城市名过滤规则。配置格式如下（示例仅列出两个国家）：

```json
{
  "countries": [
    {"name": "美国", "enabled": true, "filter": "(?i)(美国|美國|\\bUS\\b|\\bUSA\\b|United States)"},
    {"name": "日本", "enabled": true, "filter": "(?i)(日本|\\bJP\\b|\\bJPN\\b|Japan)"}
  ]
}
```

`name` 用于生成“机场·国家”组名，必须唯一；`enabled` 控制是否启用该国家，省略时为 `true`；`filter` 是节点名称的单行正则表达式，使用 Mihomo 支持的语法，建议以地区关键词的子串匹配为主。`(?i)` 表示忽略大小写，JSON 中正则的反斜杠需写成 `\\`。可增删国家或修改过滤规则，数组顺序决定每个机场的 AI 国家组顺序。禁用国家后，AI 不创建该国家组，家宽不再匹配该国家；全部禁用或设置 `countries: []` 时，两类组及其引用均不创建。模板内手写的普通地区策略组仍由模板定义。

该文件在启动或首次生成相关组时自动创建，已有文件不覆盖；每次生成读取一次，AI 和家宽使用同一份国家配置。修改后重新生成即可生效，无需重启或修改模板。文件格式或过滤规则无效时生成报错，并保留已有完整配置。无人启用 AI 和家宽时不读取该文件。

家宽关键词在 `mapping/config/residential_filter.json` 中配置，JSON 只定义名称识别，不包含国家：

```json
{
  "keywords": ["家宽"]
}
```

节点名包含任一关键词即满足家宽名称条件，不区分大小写，关键词按普通文本匹配（不是正则表达式）。可自行添加 `家寬`、`住宅`、`Residential` 等命名；仍需同时匹配 country_filter.json 中任一已启用国家的规则。文件在启动或首次生成家宽组时自动创建，已有文件不覆盖；修改后下次生成生效，无需重启。`keywords` 必须是非空字符串数组。这两个 JSON 与其他运行时配置一样不提交 Git，部署时可同步已有文件或由程序创建默认文件。

现有模板需在 `proxy-groups:` 下加入 `__PROVIDER_RESIDENTIAL_GROUPS__`（缩进两个空格），并在 AI 等入口的 `proxies:` 下加入 `__RESIDENTIAL_NODE__`（缩进六个空格）。本地 `mapping/config/template.yaml` 已在现有版本上补充这些位置；部署时也需同步这份模板。

订阅页面的“DNS 下发”默认不勾选，独立于 AI、家宽和测速开关。无人勾选时不下载订阅、不新增 DNS 策略，生成内容保持原样。勾选后在生成完整 OpenClash 配置时下载对应订阅（20 秒超时、最大 10 MiB），按机场单独 UA、全局 UA、`clash.meta` 的顺序选择 User-Agent；UA 为数组时使用第一项。

在线配置成功下载且能提取 DNS 后，原始 YAML 自动保存到 `mapping/config/providers/机场名.yaml`（例如 `providers/MESL.yaml`），目录自动创建。HTTP 403、网络错误、返回无效或不含 DNS 的在线配置时，尝试从该目录的同名本地文件提取 DNS；可手动放入从机场下载的完整配置。首次为该机场执行 DNS 下发时，原来位于 `mapping/config/机场名.yaml` 的缓存自动迁移到 `providers/`；目标已存在则优先使用目标文件，不覆盖或删除旧文件。无效在线响应不会覆盖已有有效文件。生成结果显示所用本地文件名，并在 `full_config.dns_providers` 中返回来源 `source`（`download`/`local`）及回退原因。机场名须适用于文件名，不能包含斜杠等特殊字符或与 `template.yaml` 冲突。

订阅须返回包含 `dns` 和内联 `proxies` 的完整 Clash/Mihomo YAML。优先提取 `dns.proxy-server-nameserver`，为空时使用 `dns.nameserver`；只筛选节点 `server` 中的域名，跳过 IPv4/IPv6，去重后合并到生成配置的 `dns.proxy-server-nameserver-policy`。其他 DNS 字段和 `fake-ip-filter` 保留模板值，不修改模板文件、不复制机场的全局 DNS。若模板没有 `proxy-server-nameserver`，使用模板原有 `nameserver` 或 `default-nameserver` 补齐，使定向策略生效。

在线配置不可用且本地文件不存在或无效、同一节点域名对应不同 DNS、与模板已有同域名策略冲突时，完整配置生成会报错并保留原输出。策略按域名匹配，无法区分共用同一服务器域名的不同机场。节点或 DNS 更新后需重新生成并加载完整配置；客户端仅更新代理订阅不会更新这部分策略。DNS 下发需要 PyYAML 依赖，尚未安装依赖的旧 Docker 镜像需重新构建；本次本地回退及关键词配置更新只需同步代码后重启。

#### rule_mapping 说明

```yaml
rule_mapping:
  Direct: 🌐 本机·本地直连   # 固定路由
  HighTraffic: HighTraffic   # 值=键 → 自动生成 select 策略组
  AI: AI                     # 同上，可在面板手动选节点
  Proxy: Proxy               # 对应现有的 Proxy 组
```

## 生成逻辑

### OpenClash 配置生成流程

```
点击 "生成 OpenClash"
        │
        ▼
generate_all_openclash_rules()
  ├── 规则合并：X.txt + XIP.txt → X（自动合并 IP 规则）
  └── 调用 generate_full_openclash_config()
        ├── 读取 template.yaml → base settings + proxy-groups + rule_mapping
        ├── 读取 subscribe.json → proxy-providers
        ├── 替换占位符（__ALLNODES__ 等）
        ├── 生成内联 rules（从 mapping/rules/*.txt + rule_mapping）
        └── 写入 mapping/rule-set/openclash/openclash.yaml（完整配置）
```

### 输出说明

生成的 `openclash.yaml` 是一个**自包含的完整配置**：
- 所有规则内联在 `rules:` 节，无需外部文件
- 多机场订阅节点自动聚合
- 每条规则类别有独立 select 组，可手动选节点
- 区域策略组通过正则自动过滤节点

## API 参考

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/config` | GET / POST | 获取或更新远程规则与定时任务配置 |
| `/api/subscribe` | GET / POST | 获取或保存机场订阅、全局 User-Agent 与 AI、家宽、DNS、测速开关 |
| `/api/rules` | GET | 获取规则列表 |
| `/api/rules/order` | GET / POST | 获取或更新规则显示顺序 |
| `/api/rules/create`、`/api/rules/update`、`/api/rules/delete` | POST | 管理规则文件 |
| `/api/generate`、`/api/generate/all` | POST | 生成一个或全部 SRS 规则集 |
| `/api/srs` | GET | 获取生成的 SRS 文件列表 |
| `/api/remote/status`、`/api/remote/update` | GET / POST | 查看或更新远程规则源 |
| `/api/generate/openclash/all` | POST | 生成完整 OpenClash 配置 |

## OpenClash 使用方式

### 上传完整配置

将生成的 `mapping/rule-set/openclash/openclash.yaml` 上传到 OpenClash → 配置文件管理 → 导入。所有设置（订阅、策略组、规则）内联在单个文件中。

## 维护说明

### 添加新机场

1. 在 `mapping/config/subscribe.json` 的 `providers` 数组中添加条目
2. 如果模板策略组需要引用新提供商，`mapping/config/template.yaml` 中 `__ALLNODES__` 会自动展开

### 添加新规则

1. 在 `mapping/rules/` 目录创建 `xxx.txt`
2. 在 `mapping/config/template.yaml` 的 `rule_mapping` 中添加对应条目
3. 重新生成 OpenClash 配置
