# singbox-srs-generator

一个使用 Python 标准库实现的 sing-box 规则集生成工具，支持生成 **sing-box SRS 二进制规则集** 和 **完整 OpenClash 配置文件**。

## 功能

- **规则集管理**：新建、编辑、删除、查看、手动排序 `mapping/rules/*.txt`
- **规则转换**：生成 sing-box JSON 规则文件
- **SRS 生成**：调用 `sing-box rule-set compile` 生成 `.srs`
- **OpenClash 完整配置生成**：
  - 从 `mapping/config/subscribe.json` 读取多机场订阅，生成 `proxy-providers`
  - 从 `mapping/config/template.yaml` 读取策略组结构，自动填充提供商列表
  - 从 `mapping/rules/*.txt` 读取规则，内联到 `rules:` 节（无需外部文件引用）
  - IP 规则自动合并（`Direct.txt` + `DirectIP.txt` → `Direct`）
  - 每条规则类别自动生成独立 select 策略组，可手动选节点
- **远程规则同步**：按规则集中使用到的 `geosite:` / `geoip:` 下载对应 JSON
- GitHub token 支持：通过前端配置或 Docker 环境变量 `GITHUB_TOKEN` 配置
- Docker 部署：内置端口 `9044`
- 无 Python 第三方依赖

## 目录结构

```text
.
|-- app.py                 # 后端 HTTP 服务
|-- Dockerfile
|-- docker-compose.yml
|-- docker/
|   `-- entrypoint.sh      # 容器启动与 cron 初始化
|-- mapping/               # 运行时数据（gitignored）
|   |-- bin/               # sing-box 二进制文件
|   |-- config/            # config.json、subscribe.json、template.yaml、ports.json、.env
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

### 本地运行

```bash
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
| `providers[].use_for_ai` | 是否为 AI 策略组生成“机场·国家”节点组，默认 `true` |
| `providers[].override.additional-prefix` | 节点名前缀 |

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
| `__PROVIDER_GROUPS__` | 各机场对应的 select 策略组定义（机场组） |
| `__PROVIDER_COUNTRY_GROUPS__` | 所有启用 AI 的“机场·国家”延迟测速组定义 |
| `__PROVIDER_COUNTRY_NODES__` | 所有启用 AI 的“机场·国家”组名称列表，可嵌入 AI 的 `proxies:` |
| `__RULE_GROUPS__` | 各规则类别对应的 select 策略组（rule_mapping 中值=键的条目） |
| `__PROXY_PROVIDERS__` | proxy-providers 插入位置 |

#### rule_mapping 说明

```yaml
rule_mapping:
  Direct: 🌐 本机·本地直连   # 固定路由
  HighTraffic: HighTraffic   # 值=键 → 自动生成 select 策略组
  AI: AI                     # 同上，可在面板手动选节点
  Proxy: Proxies             # 引用现有的 Proxies 组
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
| `/api/subscribe` | GET / POST | 获取或保存机场订阅、全局 User-Agent 与 AI 开关 |
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
