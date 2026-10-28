# 远程 SSH 终端部署与管理

本功能使用独立的 Apache Guacamole、guacd、PostgreSQL 和 Nginx 容器，不需要 GPU。
与 AI 助手一起由 rc21 安装器发布，但账号、数据库、启停均独立于 C-Check 审查服务。
模型仍为 Qwen2.5-Coder-32B FP8，终端功能不会替换模型。

## 环境与组件版本

部署主机需 Linux x86_64、Python 3.10+、OpenSSL 3+、Docker Engine、Compose v2 和 iproute2。
Guacamole/guacd 为 1.6.0，PostgreSQL 为 17 Alpine；完整镜像引用以随包签名的
`terminal/images.lock.json` 为准，全部固定到 SHA256 摘要，不使用 latest。
安装脚本、Compose、代理配置和镜像锁随包提供。默认在第一次部署时拉取缺少的镜像，
不重复打包模型或下载模型。需要访问锁文件中的镜像仓库及 SSH 目标地址。
可在联网主机按锁文件拉取并 `docker save`，离线主机 `docker load` 后设置
`image_policy: "offline"`，此模式缺少镜像时报错，不自动联网。

可通过 `image_sources` 为四个组件指定镜像仓库，摘要必须与锁文件完全一致。
不能通过更换仓库绕过版本锁或信任校验。Guacamole 与 guacd 为 Apache-2.0，
PostgreSQL 为 PostgreSQL License，Nginx 为 BSD-2-Clause；容器中的依赖另有各自许可证。

## 配置

在原站点 JSON 配置顶层新增 `terminal`，其余审查与模型配置保持不变：

```json
"terminal": {
  "enabled": true,
  "data_dir": "/home/lyh/c-check-terminal",
  "bind": "127.0.0.1",
  "port": 8803,
  "public_url": "https://localhost:8803/guacamole/",
  "image_policy": "online",
  "connections": [
    {
      "name": "Linux 工作站",
      "hostname": "172.26.2.250",
      "port": 22,
      "username": "lyh",
      "host-key": "ecdsa-sha2-nistp256 替换为经过核实的Base64主机公钥"
    }
  ]
}
```

在 SSH 目标的可信控制台执行 `cat /etc/ssh/ssh_host_ecdsa_key.pub` 获取公钥，
只保留类型和 Base64 两段，并通过可信渠道确认指纹。不要把未经核实的网络扫描结果直接作为信任依据。
不填写 SSH 密码、私钥或令牌，用户打开终端连接时交互输入密码。
主机公钥必须匹配 guacd 实际协商的算法；本次固定镜像与编译服务器协商使用 ECDSA。
只配置该服务器的 Ed25519 公钥而实际协商 ECDSA 时也会拒绝连接，不能通过清空 host-key 绕过。
目标地址是从 guacd 容器访问的地址，`127.0.0.1` 指容器自身，不是宿主机。
连接宿主机可使用 `host.docker.internal`，前提是其 SSH 端口可从 Docker 网桥访问。

默认只监听回环地址，生成 90 天自签名测试证书。浏览器位于另一台机器时，
可用 SSH 本地转发访问，或显式配置内网监听地址、公开 URL 和可信 TLS 证书：

```json
"bind": "172.26.2.250",
"port": 18803,
"public_url": "https://172.26.2.250:18803/guacamole/",
"tls": {
  "cert_file": "/home/lyh/c-check-tls/terminal.crt",
  "key_file": "/home/lyh/c-check-tls/terminal.key"
}
```

证书 SAN 必须覆盖 URL 的域名或 IP，私钥须属于部署者且仅所有者可读。
不要把远程终端直接暴露至公网；使用防火墙、VPN 或受控网关。
默认三段终端网络为 `172.31.240.0/26`、`172.31.240.64/26`、`172.31.240.128/26`。
部署前可通过 `subnets.data/ssh/web` 配置无冲突的私网网段。
已安装后的网段迁移不在普通 configure 操作中自动执行。

## 安装与启停

使用 rc21 包内的 `ccheck`，不要调用旧 rc16 安装器。先核对发布者公钥指纹。
终端安装也会校验整个补丁包和独立可信公钥，不会信任包内自带公钥作为初始信任依据。

```bash
cd /home/lyh/c-check-packages/release-v0.1.0-rc.21
sudo ./ccheck terminal-install \
  --config /home/lyh/c-check-site.json \
  --release "$PWD" \
  --trusted-key /home/lyh/c-check-instance/config/publisher.pem

sudo ./ccheck terminal-start --config /home/lyh/c-check-site.json
sudo ./ccheck terminal-status --config /home/lyh/c-check-site.json
sudo ./ccheck terminal-check --config /home/lyh/c-check-site.json
sudo ./ccheck terminal-stop --config /home/lyh/c-check-site.json
sudo ./ccheck terminal-logs --config /home/lyh/c-check-site.json
```

安装完成后查看安装器提示的 `state/admin-credentials.json`，终端初始用户名为
`terminal-admin`，密码随机生成。该账号与 Web 审查账号不同，首次登录后及时修改密码。
不要在截图、聊天或工单中粘贴凭据文件内容。初始凭据文件不会跟随用户修改密码而更新。

停止后再次运行 `terminal-start` 即可，数据库、账号和连接权限保留。
`ccheck stop/start` 管理审查服务，`terminal-stop/start` 管理终端，两者不会互相停止。
终端更新会保留原数据；缺失状态或数据库卷时报错，不会静默创建新账号。

## 修改地址与连接

编辑原配置中的监听 IP、端口、URL、证书或 `connections`，随后执行：

```bash
sudo ./ccheck terminal-configure --config /home/lyh/c-check-site.json
```

该命令会短暂停止终端并重新启动，请先结束活动 SSH 会话。
修改目标地址时会清除该受管连接中可能保存的 SSH 密码和私钥，避免旧凭据发给新主机。
未改名的受管连接保留连接 ID 和授权；从配置删除的受管连接会被删除。
手工创建的同名连接不会被接管，会报错要求先消除冲突。
普通终端用户需由终端管理员在 Guacamole 中授权具体连接，不自动授予所有 SSH 目标。

终端数据不包含在审查服务的备份里。生产部署须独立备份完整终端状态目录、
TLS 证书以及 PostgreSQL 数据库；不要删除 Docker 数据卷或重新初始化来解决登录问题。
