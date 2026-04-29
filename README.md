# singbox-srs-generator

一个使用 Python 标准库实现的 sing-box 规则集生成工具。

项目提供一个轻量 Web 页面，用于管理本地规则集，将 Xray / Passwall 常见 geosite 写法转换为 sing-box JSON 规则，并调用本地 `sing-box` 二进制生成 `.srs` 规则集文件。

## 功能

- 规则集管理：新建、编辑、删除、查看 `rules/*.txt`
- 规则转换：生成 sing-box JSON 规则文件
- SRS 生成：调用 `sing-box rule-set compile` 生成 `.srs`
- 远程规则同步：按规则集中使用到的 `geosite:` / `geoip:` 下载对应 JSON
- GitHub token 支持：通过 Docker 环境变量 `GITHUB_TOKEN` 配置
- Docker 部署：内置端口 `9044`
- 无 Python 第三方依赖

## 目录结构

```text
.
|-- app.py                 # 后端 HTTP 服务
|-- config/
|   |-- config.json        # 配置文件，token 默认保持为空
|   `-- .env               # Docker 环境变量
|-- Dockerfile
|-- docker-compose.yml
|-- docker/
|   `-- entrypoint.sh      # 容器启动与 cron 初始化
|-- rules/                 # 用户规则 txt
|-- rules-dat/             # 下载的 geosite / geoip JSON
|   |-- geosite/
|   `-- geoip/
|-- rule-set/              # 生成的 sing-box JSON
|   `-- srs/               # 生成的 SRS 文件
`-- web/
    `-- index.html         # 前端页面
```

## 快速开始

### Docker Compose

```bash
docker compose up -d --build
```

访问：

```text
http://localhost:9044
```

默认挂载：

- `/vol1/1000/DockerData/sing-box/rules:/app/rules`
- `/vol1/1000/DockerData/sing-box/rules-dat:/app/rules-dat`
- `/vol1/1000/DockerData/sing-box/rule-set:/app/rule-set`
- `./config:/app/config`
- `./config/.env` 通过 `env_file` 加载

### 环境变量

可以通过 `config/.env` 传入：

```env
GEOSITE_URL=https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geosite?ref=sing
GEOIP_URL=https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geoip?ref=sing
GITHUB_TOKEN=
```

`GITHUB_TOKEN` 只从环境变量读取，不会保存到 `config.json`。

### 本地 Windows 运行

项目根目录需要存在：

```text
sing-box.exe
```

启动：

```powershell
python app.py
```

访问：

```text
http://localhost:9044
```

## 配置

`config/config.json` 示例：

```json
{
  "geosite_url": "https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geosite?ref=sing",
  "geoip_url": "https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geoip?ref=sing",
  "github_token": "",
  "auto_update_enabled": false,
  "auto_update_cron": "0 4 * * *"
}
```

说明：

- `geosite_url`：远程 geosite JSON 目录地址
- `geoip_url`：远程 geoip JSON 目录地址
- `github_token`：保留为空，实际 token 使用 `GITHUB_TOKEN` 环境变量
- `auto_update_enabled`：Docker 环境下是否启用 cron 自动更新
- `auto_update_cron`：自动更新 cron 表达式

## 规则格式

规则文件保存在 `rules/{name}.txt`。

支持的域名规则：

```text
# 注释
google.com
keyword:youtube
domain:example.com
full:example.com
regexp:\.google\.com$
geosite:google
```

转换关系：

- 无前缀纯字符串 -> `domain_keyword`
- `keyword:xxx` -> `domain_keyword`
- `domain:xxx` -> `domain_suffix`
- `full:xxx` -> `domain`
- `regexp:xxx` -> `domain_regex`
- `geosite:xxx` -> 合并 `rules-dat/geosite/{xxx}.json`

支持的 IP 规则：

```text
geoip:cn
1.1.1.1
8.8.8.0/24
2001:4860:4860::8888
```

转换关系：

- IPv4 / IPv6 / CIDR -> `ip_cidr`
- `geoip:xxx` -> 合并 `rules-dat/geoip/{xxx}.json`

## 生成结果

点击前端“生成”会生成当前规则集：

```text
rule-set/{name}.json
rule-set/srs/{name}.srs
```

点击“全部生成”会遍历 `rules/*.txt` 并生成全部规则集。

## 远程规则同步

远程规则来源默认使用：

```text
https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geosite?ref=sing
https://api.github.com/repos/MetaCubeX/meta-rules-dat/contents/geo/geoip?ref=sing
```

同步逻辑只下载当前规则集中引用到的规则，例如：

```text
geosite:google
geoip:cn
```

会下载：

```text
rules-dat/geosite/google.json
rules-dat/geoip/cn.json
```

如果遇到 GitHub API rate limit，可以配置 `GITHUB_TOKEN`：

```bash
GITHUB_TOKEN=your_token docker compose up -d
```

## API

### 配置

```text
GET  /api/config
POST /api/config
```

### 规则集管理

```text
GET  /api/rules
POST /api/rules/create
POST /api/rules/update
POST /api/rules/delete
```

### 生成

```text
POST /api/generate
POST /api/generate/all
```

### 远程规则

```text
GET  /api/remote/status
POST /api/remote/update
```

## 注意事项

- 项目不使用任何 Python 第三方依赖
- 规则集名称只允许字母、数字、点、下划线和短横线
- 前端页面需要通过后端服务访问，不建议直接双击打开 HTML
- Docker 镜像构建时会从 `ghcr.io/sagernet/sing-box` 拷贝 `sing-box` 二进制，运行时不需要单独启动 sing-box 容器
- 请不要把 GitHub token 写入 `config.json` 或提交到仓库
