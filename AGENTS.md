# AGENTS.md — srsdocker 项目交接说明

> 本文件是给**接手的 AI 编程助手**的交接说明。重点说清 git 仓库里**看不到的运行时数据**、配置约定、生成流程与已知坑。
> README.md 是面向最终用户的文档；本文件补充面向开发者的内部细节。

## 1. 这是什么

项目从最初的 sing-box SRS 规则生成器，演进为 **OpenClash 完整配置生成器**。

- **输入**：机场订阅（`subscribe.json`）+ 配置模板（`template.yaml`）+ BT 端口（`ports.json`）+ 分流规则（`rules/*.txt`，Surge 格式）
- **输出**：`mapping/rule-set/openclash/openclash.yaml` —— 一个自包含的完整 OpenClash 配置（base 设置 + proxy-providers + proxy-groups + 内联 rules）
- **副产品**：sing-box SRS 二进制规则文件（用 `sing-box` 转换）
- **Web UI**（端口 `9044`）用于在线编辑规则、触发生成、查看状态
- **Docker 部署**，代码与前端通过 volume 热加载，改代码无需 rebuild

## 2. 技术栈

- **后端**：Python 3.12，**仅标准库**（`http.server.ThreadingHTTPServer`，无第三方依赖，无 requirements.txt）
- **前端**：单文件 `web/index.html`（原生 JS，无构建步骤）
- **容器**：`python:3.12-slim` + `cron`（用于定时更新远程规则集）
- **路由**：手写 `do_GET` / `do_POST`，无框架

## 3. 目录结构（⭐ 标记 = gitignored，git 里看不到）

```
srsdocker/
├── app.py                      # 后端全部逻辑（~2200 行，单文件）
├── web/index.html              # 前端单页
├── Dockerfile                  # 仅 COPY app.py / web / entrypoint.sh
├── docker-compose.yml          # 三个 volume 挂载见 §6
├── docker/entrypoint.sh        # 建 mapping 子目录 + 写 cron + 启动 python
├── README.md                   # 用户文档
├── AGENTS.md                   # 本文件
├── config/                     # 根目录下的 config（旧位置，Dockerfile 不用）
│
└── mapping/                    ⭐ 整个目录 gitignored（运行时数据/配置根）
    ├── config/
    │   ├── config.json         # 远程规则源 + 定时任务开关（见 §4.4）
    │   ├── subscribe.json      ⭐ 机场订阅配置（含真实 URL/Token，勿泄露/勿提交）§4.1
    │   ├── template.yaml       ⭐ OpenClash 配置模板 §4.2
    │   ├── ports.json          ⭐ BT/PT 直连端口 §4.3
    │   ├── order.json          ⭐ rules 显示顺序
    │   ├── .env                ⭐ 环境变量（GEOSITE_URL/GEOIP_URL/GITHUB_TOKEN）
    │   └── .env.example
    ├── rules/*.txt             ⭐ 13 个 Surge 格式规则文件（见 order.json）
    ├── rules-dat/              ⭐ geosite/geoip 下载缓存
    ├── rule-set/
    │   ├── srs/                ⭐ sing-box SRS 输出
    │   └── openclash/
    │       ├── openclash.yaml  ⭐ 最终生成产物（自包含完整配置）
    │       └── providers/      ⭐ 机场订阅缓存（生成的子配置）
    └── bin/sing-box            ⭐ SRS 转换二进制
```

**关键认知**：clone 仓库后 `mapping/` 不存在。运行时由 `entrypoint.sh` 创建目录、由 Web UI 或手动放入配置文件。`app.py` 里所有路径常量都以 `MAPPING_DIR` 为根（见 `app.py` 顶部常量定义）。

## 4. 运行时配置文件结构（gitignored，需手动了解）

### 4.1 `mapping/config/subscribe.json` — 机场订阅
```json
{
  "user_agent": "clash-verge/v2.4.5",   // 全局 UA，所有机场共用（YAML 锚点 x-ua）
  "providers": [
    { "name": "机场名", "url": "https://...", "interval": 86400 }
    // 可选字段：use_for_ai（默认 true）；use_for_latency（默认 false）；override.additional-prefix 给节点名加前缀（如 "Main："/"Minor："）
  ]
}
```
- `user_agent` 支持字符串或字符串数组（数组会展开为多个 UA 列表项）
- `providers[].use_for_ai` 为 `false` 时，该机场不会生成 AI 可选的“机场·国家”节点组
- `providers[].use_for_latency` 为 `true` 时，该机场组为 `url-test`，自动选择延迟最低节点；否则为手动 `select`
- 生成的 proxy-providers 用 YAML 锚点 `&x-ua` 去重 UA，每个 provider 用 `<<: *x-ua` 继承
- **⚠️ 含真实订阅链接，绝不能提交 git，也不能在对话/输出中泄露 URL**

### 4.2 `mapping/config/template.yaml` — OpenClash 配置模板
分两部分，由 `# ===== Generator-Only Sections =====` 分隔：

**上半部分（prelude，原样输出）**：base 设置、DNS、sniffer、`proxy-groups`
**下半部分（generator-only，不输出，只供生成器读取）**：`rule_mapping`、`custom_rules`

模板内用占位符标记自动替换的位置（见 §5）。

### 4.3 `mapping/config/ports.json` — BT/PT 直连端口
```json
{ "direct_ports": [22223, 41644, "6881-6889", "16881-16889"] }
```
- 数字（单端口）不用引号，含 `-` 的范围（字符串）要引号
- 生成时转为 `DST-PORT,<port>,DIRECT` 规则，插在 custom_rules 之后

### 4.4 `mapping/config/config.json` — 远程规则源与定时任务
```json
{
  "geosite_url": "https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geosite?ref=sing",
  "geoip_url":  "https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geoip?ref=sing",
  "github_token": "",              // 可选，GitHub API 限流时填
  "auto_update_enabled": false,    // cron 开关
  "auto_update_cron": "0 4 * * *"
}
```
`entrypoint.sh` 启动时读此文件，生成 `/etc/cron.d/singbox-srs-generator`，定时跑 `python app.py --update-remote-rules`。

## 5. 占位符系统（template.yaml 内，最近刚改名）

| 占位符 | 生成内容 | 典型位置 |
|--------|---------|---------|
| `__PROXY_PROVIDERS__` | 整个 `proxy-providers:` 块（含 x-ua 锚点 + 各机场） | prelude 顶部插入点 |
| `__PROVIDER_GROUPS__` | 各机场策略组定义（按 `use_for_latency` 生成 `select` 或 `url-test`） | proxy-groups 列表内 |
| `__PROVIDER_COUNTRY_GROUPS__` | 每个启用 AI 的“机场·国家”url-test 延迟测速组定义 | proxy-groups 列表内 |
| `__PROVIDER_COUNTRY_NODES__` | 每个启用 AI 的“机场·国家”组名称列表 | AI 的 `proxies:` 下 |
| `__ALLNODES__` | 机场名列表（`- 机场A / - 机场B ...`） | proxy-groups 的 `proxies:` 或 `use:` 下 |
| `__RULE_GROUPS__` | rule_mapping 中"键=值"条目对应的 select 组定义 | proxy-groups 列表内 |

**⚠️ `__ALLNODES__` 放在不同字段下语义不同（这是最易踩的坑）：**

```yaml
- name: HighTraffic
  type: select
  proxies:
    - ALL·延迟最低
    __ALLNODES__          # 放 proxies: → 引用同名机场组（要点进去才看到单个节点）
    - DIRECT

- name: 手动选择
  type: select
  use:
    __ALLNODES__          # 放 use: → 所有机场的所有节点直接平铺进本组
```

原因：Clash 规定 `proxy-provider` 不能被 `proxies:` 直接引用，只能走 `use:`。而 `__PROVIDER_GROUPS__` 恰好生成了和机场同名的策略组，所以 `proxies:` 下的机场名引用的是那个分组。

> 历史命名：此占位符原名 `__PROVIDERS__`（与 `__PROVIDER_GROUPS__` 太像，易混），已改为 `__ALLNODES__`。如见旧名，统一替换为新名。替换逻辑在 `app.py` `generate_full_openclash_config()` 内。

## 6. 运行与部署

### 6.1 本地运行（无 Docker）
```bash
python app.py                         # 监听 http://127.0.0.1:9044
python app.py --update-remote-rules   # 手动更新远程规则集
```
前提：`mapping/config/` 下有 subscribe.json / template.yaml 等文件，`rules/*.txt` 有规则。

### 6.2 本地 Docker
```bash
docker compose up -d --build
```
`docker-compose.yml` 三个 volume（**热加载机制**）：
- `./app.py:/app/app.py` — 改后端代码，restart 容器即可生效，**不用 --build**
- `./web:/app/web` — 改前端 HTML，刷新浏览器即可
- `./mapping:/app/mapping` — 运行时数据持久化

### 6.3 FNOS NAS 部署要点
- `docker-compose.yml` 的 `env_file` 用相对路径 `./mapping/config/.env`；在 NAS 上可改为绝对路径如 `/vol1/1000/DockerData/srsdocker/config/.env`
- 国内拉镜像可能失败（TLS 握手超时 / 401），用镜像加速器：`docker.1ms.run`（`docker.fnnas.com` 曾返回 401，已弃用）
- Dockerfile **不 COPY** 任何 gitignored 目录（`mapping/` 等），它们全靠 volume 挂载

## 7. API 端点（app.py，端口 9044）

**GET**：`/api/config` · `/api/subscribe` · `/api/rules` · `/api/rules/order` · `/api/srs` · `/api/remote/status` · `/`（前端）
**POST**：`/api/config` · `/api/subscribe` · `/api/rules/{create,update,delete,order}` · `/api/generate` · `/api/generate/all` · `/api/generate/openclash/all` · `/api/remote/update`

生成完整 OpenClash 配置的入口：`POST /api/generate/openclash/all`（前端"生成 OpenClash"按钮触发）。

## 8. 生成流程数据流

`generate_full_openclash_config()`（app.py ~1796 行起）：

1. `read_template()` 解析 template.yaml → 拆出 prelude + rule_mapping + custom_rules
2. `load_subscribe_config()` 读 subscribe.json → providers + 全局 UA
3. 占位符替换（`__PROXY_PROVIDERS__` / `__PROVIDER_GROUPS__` / `__PROVIDER_COUNTRY_GROUPS__` / `__PROVIDER_COUNTRY_NODES__` / `__ALLNODES__` / `__RULE_GROUPS__`）
4. `load_ports_config()` 读 ports.json → 生成 BT/PT 直连 DST-PORT 规则
5. `generate_rules_yaml()` 拼装内联 rules 段：
   - 非 MATCH 的 custom_rules 在前
   - BT/PT 端口规则
   - rule_mapping 按 template 顺序展开（**不排序**）
   - **MATCH 类规则必须放最后**（否则会 shadow 后续所有规则）
6. 组装 prelude + proxy-providers + proxy-groups + rules → 写 `openclash.yaml`

## 9. 重要约定与坑

- **规则文件格式**：`rules/*.txt` 是 **Surge 格式**（`geosite:` / `domain:` / `full:` / `keyword:` / `regexp:` / `geoip:` 前缀），生成时转为 Classical 规则（`GEOSITE,` / `DOMAIN-SUFFIX,` / `IP-CIDR,...,no-resolve` 等）
- **IP 规则自动合并**：`DirectIP.txt` 的 IP 规则会合并进 `Direct.txt` 生成的同一条规则集，`DirectIP` 不单独成组（`get_merged_rule_names()` 检测 `XIP.txt` → 合并进 `X.txt`）。rule_mapping 里的 IP 条目会被过滤掉
- **MATCH 必须最后**：custom_rules 里若有 `MATCH`，不能和其他规则混排，会 shadow 全部后续规则
- **UA 锚点用 `x-ua`**：曾用 `_ua`（前导下划线某些 OpenClash 版本异常），已改
- **占位符别写进注释**：模板注释里若含占位符文本会被误替换
- **多行替换缩进**：`__ALLNODES__` / `__PROVIDER_GROUPS__` / `__PROVIDER_COUNTRY_GROUPS__` / `__PROVIDER_COUNTRY_NODES__` 替换时只有首行继承模板缩进，后续行由生成逻辑补齐缩进
- **fake-ip 模式**：DNS 必须有 `default-nameserver`，否则 OpenClash 拒绝加载
- **empty proxies**：proxy-groups 的组不能有空的 `proxies:`，模板里不要留空段

## 10. 当前状态与待办

- **未提交改动**：`app.py` + `README.md` 有占位符改名（`__PROVIDERS__` → `__ALLNODES__`）的未提交改动，待 commit/push
- **已知问题 — ImmTelecom 订阅返回 0 节点**：换过多个 UA（clash-verge / openclash）无效；同 URL 在 OpenClash 直接订阅能拉到节点，但经 proxy-providers 拉取为 0。根因未定位，怀疑订阅 URL 的编码/格式问题。当前用户手动上传订阅文件绕过
- **subscribe.json 当前有 5 个机场**：ImmTelecom、CreamData、良心云、一分机场、魔戒（CreamData 为新增；当前未用 `additional-prefix` override）
- **template.yaml 当前状态**：已回滚到"在所有节点中选择"功能加入前的版本——7 个规则策略组（HighTraffic/AI/PrimaryProxy/TWSensitive/Crypto/China/Proxy）只有 `proxies:` 无 `use:`，节点按机场分组间接选择。如需恢复"所有节点平铺"，给这些组加 `use: __ALLNODES__`

## 11. 改动 checklist（改 app.py 时注意）

- 改生成逻辑后，本地 `python app.py` 起服务，点前端"生成 OpenClash"，检查 `mapping/rule-set/openclash/openclash.yaml`
- 改模板占位符名 → 同步改 `app.py` 替换逻辑 + `template.yaml` + `README.md`
- 加新规则文件 → 更新 `order.json`，确认 `rule_mapping` 有对应映射
- 改 Dockerfile COPY → 切勿 COPY 任何 gitignored 目录（会 build not found）
