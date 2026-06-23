# singbox-srs-generator

一个使用 Python 标准库实现的 sing-box 规则集生成工具，支持生成 **sing-box SRS 二进制规则集** 和 **完整 OpenClash 配置文件**。

## 功能

- **规则集管理**：新建、编辑、删除、查看、手动排序 `rules/*.txt`
- **规则转换**：生成 sing-box JSON 规则文件
- **SRS 生成**：调用 `sing-box rule-set compile` 生成 `.srs`
- **OpenClash 完整配置生成**：
  - 从 `config/subscribe.json` 读取多机场订阅，生成 `proxy-providers`
  - 从 `config/template.yaml` 读取策略组结构，自动填充提供商列表
  - 从 `rules/*.txt` 读取规则，内联到 `rules:` 节（无需外部文件引用）
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
|-- bin/                   # sing-box 二进制文件
|   |-- sing-box           # Linux
|   `-- sing-box.exe       # Windows
|-- config/
|   |-- config.json        # 配置文件
|   |-- subscribe.json     # 机场订阅配置（私人，不提交 Git）
|   |-- template.yaml      # OpenClash 配置模板（私人，不提交 Git）
|   |-- order.json         # 规则集手动排序
|   |-- .env               # Docker 环境变量
|   `-- .env.example       # 环境变量示例
|-- Dockerfile
|-- docker-compose.yml
|-- docker/
|   `-- entrypoint.sh      # 容器启动与 cron 初始化
|-- rules/                 # 用户规则 txt
|-- rules-dat/             # 下载的 geosite / geoip JSON
|   |-- geosite/
|   `-- geoip/
|-- rule-set/              # 生成的输出
|   |-- srs/               # 生成的 SRS 文件
|   `-- openclash/         # 生成的 OpenClash 配置
|       |-- openclash.yaml     # 完整配置（可直接上传 OpenClash）
|       `-- providers/         # 古典文本规则文件（可选引用）
|-- web/
|   `-- index.html         # 前端页面
`-- .gitignore
```

## 快速开始

### Docker Compose

```bash
# 1. 编辑机场订阅配置
vim config/subscribe.json    # 填入真实的订阅 URL

# 2. 编辑 OpenClash 模板（按需调整策略组和规则映射）
vim config/template.yaml

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

### `config/subscribe.json`

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
| `providers[].override.additional-prefix` | 节点名前缀 |

### `config/template.yaml`

OpenClash 配置模板，**私人配置，不提交 Git**。包含：
- 基础设置（端口、DNS、Sniffer 等）
- 策略组结构（proxy-groups）
- 规则映射（rule_mapping）
- 自定义规则（custom_rules）

模板中使用占位符自动替换：

| 占位符 | 替换为 |
|--------|--------|
| `__PROVIDERS__` | subscribe.json 中的提供商名称列表 |
| `__PROVIDER_GROUPS__` | 各提供商对应的 select 策略组 |
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
  ├── 生成 .list 古典文本文件（可选，用于外部引用）
  └── 调用 generate_full_openclash_config()
        ├── 读取 template.yaml → base settings + proxy-groups + rule_mapping
        ├── 读取 subscribe.json → proxy-providers
        ├── 替换占位符（__PROVIDERS__ 等）
        ├── 生成内联 rules（从 rules/*.txt + rule_mapping）
        └── 写入 rule-set/openclash/openclash.yaml（完整配置）
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
| `/api/rules` | GET | 获取规则列表 |
| `/api/rules/generate` | POST | 手动触发规则生成 |
| `/api/rules/update-rules` | POST | 更新远程规则源 |
| `/api/rules/srs-files` | GET | 获取所有 SRS 文件列表 |
| `/api/generate/openclash/all` | POST | 生成完整 OpenClash 配置 |
| `/api/health` | GET | 健康检查 |

## OpenClash 使用方式

### 方式一：上传完整配置（推荐）

将生成的 `rule-set/openclash/openclash.yaml` 上传到 OpenClash → 配置文件管理 → 导入。所有设置（订阅、策略组、规则）内联在单个文件中。

### 方式二：引用规则文件

在 Luci 面板的「配置文件覆写」→「规则集」中添加：

```yaml
rule-providers:
  Direct:
    type: file
    behavior: classical
    format: text
    path: ./rule-set/openclash/providers/Direct.list
```

## 维护说明

### 添加新机场

1. 在 `config/subscribe.json` 的 `providers` 数组中添加条目
2. 如果模板策略组需要引用新提供商，`template.yaml` 中 `__PROVIDERS__` 会自动展开

### 添加新规则

1. 在 `rules/` 目录创建 `xxx.txt`
2. 在 `template.yaml` 的 `rule_mapping` 中添加对应条目
3. 重新生成 OpenClash 配置
