# 8848 只读 MCP 部署与 ChatGPT 接入

只读 MCP 复用现有 SQLite 数据、8848 策略计算和持仓汇总计算，独立监听本机 8849。
现有网页继续在 8848 运行，MCP 不导入网页应用、不启动后台抓取线程，也不公开网页写接口。
公网域名通过 Cloudflare Tunnel 转发到 8849，账户数据由固定 Bearer Token 或 OAuth 保护，无需另开身份服务。
个人自用、固定几个客户端时优先使用下面的固定 Token 方式，不需要账户授权密码、回调地址或修改本机 OAuth 配置。

## 能读取什么

| 工具 | 内容 |
|---|---|
| `get_watchlist_snapshot(offset=0, limit=30)` | 自选股分页，缓存行情、8848 信号、阶段关键价位，单页最多50只 |
| `get_stock_snapshot(code)` | 单只股票或 ETF 的上述数据，支持六位代码、`sh600519`、`600519.SH` |
| `get_portfolio_overview()` | 持仓、现金、缓存估值、盈亏；不含券商可卖数量 |
| `get_watch_events(after_id=0, limit=30)` | 按 ID 升序读取提醒，单页最多100条，不标记已读 |
| `get_service_status()` | 缓存最新日期、分钟快照时间、最新事件 ID、数据库可读性 |
| `get_kline_data(code, period, ...)` | ETF D/60min/15min 独立缓存、前复权 SMA55/233 与原始审计字段；不抓行情 |

ETF K 线需要显式运行独立采集器，详见 [ETF K线数据与验收边界](etf-kline-mcp.md)。未初始化或权限不足时明确返回未就绪，不从旧分钟报价表补造。

所有工具均需 `stocks:read` 权限。没有写入工具、任意 SQL、下单、行情强制刷新或 AI 调用。
业务数据库使用 SQLite `mode=ro` 和 `query_only`，不存在时不会创建文件；启用 OAuth 时独立的 OAuth 数据库需要写权限。
仅固定 Token 模式不会创建或使用 OAuth 数据库。
MCP 不执行业务库迁移，升级部署时应先让主 Web 服务正常启动并完成原有初始化。

返回中的 `generated_at` 是响应生成时间，**不是行情时间**。必须结合 `source`、`observed_at`、`stale` 使用：

- 优先取较新的分钟快照，超过180秒或明显来自未来就标记过期。休市后过期是正常现象，不代表服务必然故障。
- 日记录只有日期精度，始终标记过期；它可能是盘中查询留下的数据，不保证为最终收盘价。
- 没有行情就返回 `missing`，不能填成0元或伪装成实时价格。
- 持仓估值有任何缺价时，`valuation_complete=false`，账户总估值和汇总盈亏返回 `null`，逐股已知数据仍保留。
- 盈亏百分比沿用网页的持仓等权平均，不是账户加权收益率。非今日报价不计算“今日盈亏”。
- 主服务未启用某只股票的分钟盯盘时，其分钟缓存可能不更新；MCP 不替主服务补抓。
- 自选股列表每次读取项目根 `.env`，网页增删自选股后不需要重启 MCP；OAuth 配置变更则需要重启。
- `get_service_status` 不声称知道另一个进程里的后台线程是否存活。

## 个人自用最快连接方式

在服务器项目根目录生成一个随机 Token，每个客户端可单独生成一个：

```bash
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

编辑项目根 `.env`，保留原来其他业务配置，增加或修改以下项。域名换成实际 Tunnel 域名，Token 换成上面输出的完整值：

```dotenv
MCP_PUBLIC_URL=https://mcp.example.com
MCP_BEARER_TOKENS=替换为真实随机Token

# 仅固定 Token 模式不需要 OAuth；清空旧的这三项，避免残留半套配置
MCP_CLIENT_SECRET=
MCP_REDIRECT_URIS=
MCP_OWNER_PASSWORD_HASH=
```

多个 Token 使用英文逗号分隔，每个32–256字符，仅含字母、数字、下划线或短横线。
它们都只能调用同样的6个只读工具，没有写权限，不是用户/角色权限系统。
固定 Token 没有自动过期；从此列表删除对应值并重启 MCP 即撤销，其他 Token 不受影响。
如仍需要 ChatGPT OAuth，可保留原来完整的 OAuth 三项，和固定 Token 同时生效；不能只保留半套 OAuth 配置。
修改固定 Token 列表不会让已有 OAuth 授权失效。不要把 OAuth 客户端密钥复用为固定 Token。

按后面的启动与 Tunnel 步骤让服务运行后，在 DOT/桌面端“连接至自定义 MCP”的界面填写：

| 字段 | 填写 |
|---|---|
| 名称 | `stock8848` |
| 类型 | 流式 HTTP |
| URL | `https://mcp.example.com/mcp` |
| Bearer 令牌环境变量 | 留空（使用下面的固定标头即可） |
| 标头的键 | `Authorization` |
| 标头的值 | `Bearer 真实Token`（Bearer后面一个空格，只选列表中的一个Token） |
| 来自环境变量的标头 | 留空 |

保存并重新加载连接，**不需要点 OAuth 登录，也不需要18849回调**。如果之前加过本机 OAuth 子段，固定标头方式不需要它。
“Bearer 令牌环境变量”字段接收的是客户端进程里的环境变量**名字**，不是 Token 本身；服务器 `.env` 不会自动成为 Mac 应用的环境变量。
为了避免桌面应用环境变量继承问题，个人自用直接用上述固定标头最省事。不要同时配置两个 Authorization 来源。
标头值会保存在客户端配置中，应像密码一样保管；Token 不放 URL、不放任务提示词、不提交到 Git。

要做命令行验收，可先在自己的终端安全输入 Token，再请求工具列表（不会把 Token 字面值写入命令历史）：

```bash
read -r -s -p 'Bearer Token: ' MCP_TEST_TOKEN; echo
curl --config - <<EOF
url = "https://mcp.example.com/mcp"
header = "Authorization: Bearer ${MCP_TEST_TOKEN}"
header = "Accept: application/json, text/event-stream"
header = "Content-Type: application/json"
data = "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}"
EOF
unset MCP_TEST_TOKEN
```

上例使用 Bash 的 `read` 语法；可在服务器 Bash 终端执行。预期返回6个工具；无 Token 或错误 Token 应返回401。
后文第一节和第四节的 OAuth 配置，只在需要 OAuth 客户端时操作，**固定 Token 用户可跳过**。

## 一 可选的 OAuth 根环境配置

以下 `/opt/8848` 是**服务器部署目录示例**，全部替换成实际目录。不要覆盖已有 `.env`，也不要写到 `routes/.env`。

```bash
cd /opt/8848
uv sync --frozen
uv run python -m scripts.mcp_admin password-hash
```

输入一个至少16字符的独立账户授权密码。终端只输出 `MCP_OWNER_PASSWORD_HASH=...`，把完整一行加入根 `.env`。
这个密码只在你自己的域名授权页面输入，**不要交给 ChatGPT、写进任务提示词或放在 URL 中**。

另生成客户端密钥，保存到根 `.env`，并在 ChatGPT 的 OAuth 客户端配置中使用：

```bash
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

根 `.env` 新增以下配置（用真实值替换示例，不包含尖括号）：

```dotenv
MCP_PUBLIC_URL=https://mcp.example.com
MCP_CLIENT_ID=8848-chatgpt
MCP_CLIENT_SECRET=<刚生成的客户端密钥>
MCP_REDIRECT_URIS=https://chatgpt.com/connector_platform_oauth_redirect
MCP_OWNER_PASSWORD_HASH=<password-hash 命令输出的 scrypt$... 完整值>
```

注意：

- `MCP_PUBLIC_URL` **不带** `/mcp`、查询参数或子路径，使用专用域名。
- 上面的 ChatGPT 回调是符合 issuer 标识要求时的稳定回调地址，作为初始配置；**最终以该连接管理页实际显示的精确地址为准**。
  如果显示 `https://chatgpt.com/connector/oauth/...` 或其他回调，复制完整值修改 `.env`，重启 MCP，再授权；不能填通配符。
- 仅预注册这一个私人客户端，不支持匿名动态注册或自动读取 CIMD。客户端需允许填写 OAuth Client ID 和 Secret。
- 多个精确回调地址可用英文逗号分隔，属于同一个客户端身份；不是多用户权限隔离。
- 默认 OAuth 数据库为项目根 `mcp_auth.db`，可用绝对路径 `MCP_AUTH_DB` 修改，**不能指向 `stock_cache.db`**。
- 使用与主服务相同的系统时区 `Asia/Shanghai`；MCP 输出显式携带 `+08:00`。

配置文件只允许服务用户读取：

```bash
chmod 600 /opt/8848/.env
```

## 二 启动独立 MCP 进程

```bash
cd /opt/8848
uv run uvicorn mcp_app:create_app --factory --host 127.0.0.1 --port 8849 --workers 1 --no-access-log --no-proxy-headers
```

**不要使用 `0.0.0.0`，不要对公网开放 8849。** 没有有效固定 Token 或完整 OAuth 配置时，进程会拒绝启动，不会退化成匿名访问。
关闭访问日志是为了避免授权请求参数进入常规 URL 日志；不要开启 HTTP 调试抓包或记录表单、Cookie、Authorization。

本机检查需要显式使用公网 Host（代码会验证 Host）：

```bash
curl -sS -H 'Host: mcp.example.com' http://127.0.0.1:8849/.well-known/oauth-authorization-server
curl -i -H 'Host: mcp.example.com' http://127.0.0.1:8849/mcp
```

预期：第一个返回元数据；第二个返回 `401`，且 `WWW-Authenticate` 包含 `resource_metadata` 和 `stocks:read`。
直接访问 `http://127.0.0.1:8849` 且不传公网 Host 返回 `421` 是预期行为。

### Linux 常驻运行示例

先完成 `uv sync --frozen`，再创建 `/etc/systemd/system/8848-mcp.service`。
将示例 `stock` 替换为实际运行主服务的非 root 用户，并替换所有目录：

```ini
[Unit]
Description=8848 read-only MCP
After=network.target

[Service]
Type=simple
User=stock
WorkingDirectory=/opt/8848
Environment=TZ=Asia/Shanghai
ExecStart=/opt/8848/.venv/bin/uvicorn mcp_app:create_app --factory --host 127.0.0.1 --port 8849 --workers 1 --no-access-log --no-proxy-headers
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now 8848-mcp
sudo systemctl status 8848-mcp
```

服务用户需要读取根 `.env` 和业务 SQLite，写入独立的 `mcp_auth.db` 及现有 `app.log`。
不要把主服务的 `start.sh` 改成只启动 MCP；两个进程各司其职。

## 三 配置 Cloudflare Tunnel

已有可用 Tunnel 且 `cloudflared` 能访问此服务器时，复用它并新增域名映射即可。

### 控制台管理的 Tunnel

1. 打开 Cloudflare 的 Tunnels，选择已有 Tunnel，新增公开应用路由。
2. Hostname 填 `mcp.example.com`，Service 填 `http://127.0.0.1:8849`，不要加 `/mcp` 路径限制。
3. Additional application settings → HTTP Host Header 设置为 `mcp.example.com`，须与 `MCP_PUBLIC_URL` 一致。
4. 保存 DNS 和路由后，外部通过 HTTPS 访问该域名。Tunnel 回源同机 HTTP 不需要再给 Uvicorn 配证书。

若 `cloudflared` 在容器或另一台机器，`127.0.0.1` 指向的是它自身，不是应用服务器。
此时应调整为可达的私有地址并限制访问来源，或把 Tunnel 代理移到应用同机；不要为了省事把 8849 暴露到公网。

### 本地配置文件管理的 Tunnel

在已有 `ingress` 的最终兜底规则**之前**加入以下条目，保留原 SillyTavern 等路由和 Tunnel 凭据不变：

```yaml
ingress:
  # 保留已有路由
  - hostname: mcp.example.com
    service: http://127.0.0.1:8849
    originRequest:
      httpHostHeader: mcp.example.com
  - service: http_status:404
```

在 Cloudflare DNS 添加与现有 Tunnel 相同目标的 CNAME：`mcp` → `<Tunnel UUID>.cfargotunnel.com`，
或使用你现有的 `cloudflared tunnel route dns` 管理方式，然后验证配置并重启原 `cloudflared` 服务。
上面只是新增路由示例，**不要拿它覆盖整份现有配置**。

### 访问与缓存策略

- **不要给 MCP 域名套一层需要网页登录、邮箱验证码或浏览器挑战的 Cloudflare Access 策略。** 机器身份由本服务 OAuth 验证；
  人工网页可以用另一域名 `stock.example.com` 加 Access，仍然指向8848。
- 不要求 MCP 客户端携带 `CF-Access-Client-Id` 等额外头。若有跨子域 Access 通配策略，先将 MCP 域名独立处理。
- 对这个专用 MCP 域名设置 Cache Rule 为 Bypass；不要对 OAuth 或 MCP 使用 Cache Everything。
- 若现有 WAF/Bot 规则返回验证码，只为此域名调整相应挑战规则，不要全站关闭安全策略。保留限流、防护与 HTTPS。
- 路由整个专用域名到8849，因为除了 `/mcp`，OAuth 还需要 `/authorize`、`/consent`、`/token`、`/revoke` 和 `/.well-known/...`。
  此进程没有网页写路由，其余路径为404。
- Tunnel 验证完后关闭公网直连8848，或至少限制来源；MCP 的鉴权**不会顺便保护**原来裸露的主网页端口。

外部验收：

```bash
curl -sS https://mcp.example.com/.well-known/oauth-authorization-server
curl -sS https://mcp.example.com/.well-known/oauth-protected-resource/mcp
curl -i https://mcp.example.com/mcp
curl -i -X POST https://mcp.example.com/update_portfolio
```

依次应为：OAuth JSON、受保护资源 JSON、`401`、`404`。
不能出现 Cloudflare 登录 HTML、验证码页或意外重定向到主网页。

## 四 在 ChatGPT 配置连接

### ChatGPT 网页插件连接

在账号可用的自定义 MCP/插件连接入口中新增私人连接。
下表说明需要对应的配置，不表示不同版本界面都会逐项提供同名输入框：

| 字段 | 值 |
|---|---|
| Server URL | `https://mcp.example.com/mcp` |
| Authentication | OAuth |
| Client ID | 根 `.env` 中的 `MCP_CLIENT_ID` |
| Client Secret | 根 `.env` 中的 `MCP_CLIENT_SECRET`，不是账户授权密码 |
| Scope（如有字段） | `stocks:read` |

服务会提供端点发现元数据，不需要手工把端点改成其他路径。Token endpoint 使用 `client_secret_post`。
此服务使用预注册客户端，不支持 DCR/CIMD；需要能提供客户端凭据的连接方式。
先核对连接页面显示的回调地址与根 `.env` 完全一致；修改配置后重启 `8848-mcp` 并刷新客户端连接。

点击连接会在浏览器打开你自己的 `mcp.example.com` 授权页。检查域名与所列只读权限，输入账户授权密码并允许访问。
密码只发送给自己的服务器；ChatGPT 保存 OAuth 令牌，不需要知道这个密码。后续读取不需要每次再输入密码。

### 桌面端自定义 MCP 配置

如果界面只显示“名称、STDIO/流式 HTTP、URL、Bearer 令牌环境变量、标头、来自环境变量的标头”，
这是传输配置表单，不是上面的网页插件 OAuth 表单。它不能直接填写本服务要求的预注册客户端凭据。
`MCP_CLIENT_SECRET` 是 OAuth 客户端密钥，**不是 Bearer Access Token**，不能混填。

在该表单先保存：

| 界面字段 | 填写 |
|---|---|
| 名称 | `stock8848`（后续配置段使用相同名称） |
| 类型 | 流式 HTTP |
| URL | `https://mcp.example.com/mcp` |
| Bearer 令牌环境变量 | 留空，`MCP_BEARER_TOKEN` 如为灰色占位提示不必填写 |
| 标头 / 来自环境变量的标头 | 留空 |

此时可能显示未认证或连接错误，不代表只需再找一个客户端密钥输入框。
接下来在**运行桌面应用的电脑**补充本机 Codex 配置，不是在服务器执行：

1. 打开本机 Codex 的 `config.toml`，macOS 默认路径示例为 `/Users/你的用户名/.codex/config.toml`。
2. 找到刚保存的 `[mcp_servers.stock8848]` 段，不要重复创建同名段，也不要覆盖其他配置。
3. 为它添加以下 OAuth 子段，密钥与服务器根 `.env` 相同：

```toml
[mcp_servers.stock8848.oauth]
client_id = "8848-chatgpt"
client_secret = "替换为服务器的MCP_CLIENT_SECRET"
callback_url = "http://127.0.0.1:18849/callback"
callback_port = 18849
```

`callback_url` 的端口与 `callback_port` 必须相同。18849是桌面电脑的本机回调监听端口，
**不是服务器8849，不需要新增 Tunnel，也不要对公网开放**。不要把这个回调当成 MCP 的服务地址。

服务器的根 `.env` 保留原 ChatGPT 回调，并追加这个精确桌面回调：

```dotenv
MCP_REDIRECT_URIS=https://chatgpt.com/connector_platform_oauth_redirect,http://127.0.0.1:18849/callback
```

重启服务器 MCP，再在桌面端重新加载 MCP 配置，选择 Authenticate/认证；或在本机终端执行：

```bash
codex mcp login stock8848 --scopes stocks:read
```

浏览器打开的是服务器 HTTPS 授权页，输入账户授权密码后才回到本机监听地址；原始密码不填写到本机配置中。
客户端密钥以明文保存在本机 `config.toml`，需要限制该文件权限，不能提交或贴到聊天中。
本服务始终精确匹配白名单中的回调 URI/端口，不接受随机端口；因此这里显式固定18849。
新增回调会改变授权配置，原有连接需要重新授权一次。

官方文档区分了[桌面传输配置与 OAuth 预注册客户端配置](https://learn.chatgpt.com/docs/extend/mcp#oauth-client-registration-and-callbacks)。
仅本机 MCP 连接成功，不等于 Dot 云端已获得该连接；还需在实际运行环境检查可用工具。

### 功能验收

先测试三个请求：

1. “读取 8848 服务状态，说明缓存更新到什么时候。”
2. “读取我的自选股，区分缺失、过期和新鲜数据，不把响应生成时间当行情时间。”
3. “从 after_id=0 分页读取提醒；不要修改已读状态，记录 next_after_id。”

再测试一次短期定时任务。普通聊天能调用不代表该账号的后台任务权限已验证；确认无人操作时也能读取，再改为正式收盘任务。
桌面本地任务仍可能依赖本机和客户端在线，云端任务则依赖账号/工作区可用的已连接工具权限。

DOT 可直接用固定 Bearer 标头连接；只有选择本节 OAuth 方式时，才需要预注册 OAuth 客户端及 PKCE。
这个实现面向个人私用，不是公开多用户股票数据服务，也没有公开发布插件的相关页面和审核配置。

## 五 授权有效期与撤销

- 固定 Bearer Token 不自动过期；从根 `.env` 的 `MCP_BEARER_TOKENS` 中移除对应值并重启 MCP，即可撤销。
  下面的期限与撤销命令只针对 OAuth，不影响固定 Token。
- 授权请求5分钟有效，授权码2分钟且只能用一次，必须通过 S256 PKCE 校验。
- Access Token 有效1小时；Refresh Token 每次刷新轮换，整个授权自首次签发起最多30天，然后需要重新授权。
  这意味着长期定时任务要留意授权到期，不是永久无人维护。
- 重放已轮换的 Refresh Token 会撤销整个令牌族，需重新连接；不要手工重复发送刷新请求。
- `mcp_auth.db` 权限自动设为0600，授权码和令牌只保存摘要，重启服务不会丢失有效授权。
- 修改域名、客户端密钥、密码哈希或回调白名单并重启，会使旧配置下的授权不可用。
- 仅在客户端界面断开连接不应被当成已确认完成服务器侧撤销。紧急撤销全部授权：

```bash
cd /opt/8848
uv run python -m scripts.mcp_admin revoke
```

该命令只清除独立 OAuth 库中的授权请求、授权码和令牌，不删除持仓、现金、行情、提醒或网页记录。
`mcp_auth.db` 仍是敏感文件，不要提交、对外下载或与他人共享备份；根 `.env` 中的客户端密钥和密码哈希也需妥善保管。

## 常见问题

| 现象 | 检查 |
|---|---|
| 本机或 Tunnel 返回421 | Host 是否精确为 `MCP_PUBLIC_URL` 的域名；检查 HTTP Host Header |
| 返回401 | 未授权、令牌到期或已撤销；MCP 匿名请求返回401本来就是正常现象 |
| 授权后显示 invalid_request | 核对精确回调、`resource=https://mcp.example.com/mcp`、只读 scope 和 S256 PKCE |
| ChatGPT 提示 issuer 不一致 | 元数据 issuer、authorization_servers 和返回的 iss 必须完全一致；不要在代理中重写这些 JSON |
| 返回403 | 令牌 scope 不匹配、错误密码或不受信任的 Origin；不要为解决403开放匿名访问 |
| 返回429 | 账户密码尝试最多10次/15分钟，OAuth端点总计最多120次/分钟；等到窗口恢复，检查是否有错误重试循环 |
| 工具返回数据暂不可用 | 主服务是否完成初始化；服务用户能否读业务库；不要把坏路径改成自动创建空库 |
| 工具可连接但数据旧 | 主服务盯盘开关、历史数据拉取和缓存时间；MCP 自身不会抓新行情 |

## 协议参考

- [OpenAI MCP 鉴权要求](https://developers.openai.com/plugins/build/auth)：预注册客户端、PKCE、resource 和精确 issuer/callback。
- [OpenAI 连接与测试](https://developers.openai.com/plugins/deploy/connect-chatgpt)：连接后刷新工具元数据并重新测试。
- [MCP 授权规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)。
- [Cloudflare 域名路由](https://developers.cloudflare.com/tunnel/concepts/routing/)。
- [Cloudflare HTTP Host Header](https://developers.cloudflare.com/tunnel/reference/origin-parameters/)。
- [ChatGPT 定时任务](https://learn.chatgpt.com/docs/automations?surface=app)。
