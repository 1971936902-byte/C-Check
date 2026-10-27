# C-Check 独立远程终端

本组件提供 Apache Guacamole 网页 SSH，不依赖 C-Check API、数据库、模型或 GPU。
C-Check 的“使用帮助”下方新增管理员入口，在新窗口打开独立终端。首版采用独立登录，**不是单点登录**。

## 1. 部署结构与边界

- `ccheck-terminal`：Guacamole、guacd、PostgreSQL、HTTPS Nginx，共四个容器。
- `ccheck-terminal-preview`：可选的纯前端模拟数据预览，独立项目，不启动后端或模型。
- 独立数据库卷 `ccheck-terminal_database`，不要与 C-Check 的数据库混用。
- 默认仅监听宿主机 `127.0.0.1:8803`，通过 SSH 隧道访问。
- SSH 连接由 guacd 发起；目标机器需要 SSH 服务，网关必须能访问目标端口。
- 终端具有所登录 Linux 账号的真实权限；普通账号仍可能具有 sudo、Docker/LXD 等高权限组权限，需管理员审查。
- 不挂载 Docker socket，不启用特权容器，不挂载宿主机根目录，不请求 GPU。
- 停止 C-Check 不会停止此独立项目。同机 Docker 或整机关闭仍会导致终端失联，保留原生 SSH/云控制台。

## 2. 前期准备

已验证：Ubuntu 22.04 x86_64、Python 3.10、Docker 29.1.3、Compose v2.39.4。
需要 Python 3、OpenSSL、`ip` 命令、Docker Engine 和支持 `up --wait` 的 Compose v2。
脚本无额外 Python 包依赖。镜像首次下载需要网络，之后按 digest 复用已有镜像。

检查服务器空闲内存、磁盘、端口、路由与已有容器。四个服务配置的内存上限合计约 1.9GiB，
这不是实际用量或整机最低要求，仍需给宿主机与其他业务预留空间。
脚本不会安装/升级 Docker、重启 Docker、修改 SSH 服务配置或停止其他项目。

三个默认子网分别为 `172.31.240.0/26`、`172.31.240.64/26`、`172.31.240.128/26`。
初始化时会检查与宿主机路由、已有 Docker 网络的重叠；冲突时使用 init 的三个 subnet 参数指定其他网段。
数据库网络为 internal；Web 网络为了兼容 Docker 29 的端口发布不是 internal，但仅网关发布 loopback 端口。
正常终端用户只能使用管理员授权的预置连接。当前未实施目的地址层面的主机防火墙白名单；
终端管理员仍可配置其他目标。需要强制网络隔离的环境应在网关出口增加防火墙策略。

## 3. 首次安装

把本目录完整部署到 `/opt/c-check-terminal`，包括 `images.lock.json`。
普通部署不要执行 `lock`，它是维护者更新镜像锁定清单时才使用的命令。

```bash
cd /opt/c-check-terminal
sudo python3 terminal.py init
sudo python3 terminal.py start
sudo python3 terminal.py status
```

初始化会生成随机数据库密码、随机管理员密码及有效期 90 天的 localhost 自签名测试证书。
数据库初始化时已更换官方默认账号及密码，网页服务启动前即完成处理。
管理员账号是 `terminal-admin`。在服务器可信 SSH 会话中读取初始密码：

```bash
sudo cat /opt/c-check-terminal/state/admin-credentials.json
```

不要把上述文件、整个 `state`、数据库导出或私钥提交 GitHub。
`state` 为宿主机 root-only 目录，内部部分只读文件为 0444，供不同容器服务 UID 读取；不能放宽父目录权限。
已有 state 或项目数据卷时，init 会拒绝覆盖，防止重置已有账号、证书和数据库。
初始化部分失败时先保留并检查 state，不要直接删除重跑。

镜像固定在 `images.lock.json` 中，实际运行使用 `repository@sha256:...`，不使用浮动标签启动。
本次 Nginx 通过 ECR Public 镜像源获取，digest 已与 Docker Hub 官方 `nginx:1.28-alpine` 比对一致。
后续更新应显式评估上游安全公告、更新锁文件、备份数据库并完成回归；固定 digest 不等于永久无需安全升级。

## 4. 本次测试服务器访问

SSH 端点：`180.127.11.169:26152`，备用线路 `223.109.239.11:26152`。
在自己的电脑上执行以下命令，保持窗口运行，SSH 密码按提示输入，不放进命令：

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 18843:127.0.0.1:8803 -L 18844:127.0.0.1:8804 -p 26152 vipuser@180.127.11.169
```

- 真实终端：`https://localhost:18843/guacamole/`
- C-Check 前端集成预览：`https://localhost:18844/help`
- 预览是模拟数据模式，不连接真实 C-Check 后端，不验证模型任务。预览账号 `admin` / `admin12345678` 不是终端管理员账号。
- 点击“远程终端”后使用独立 `terminal-admin` 登录。
- 选择 `CPU test server`，终端会提示输入 Linux `vipuser` 的 SSH 密码；该密码未保存在 Guacamole 连接配置中。
- 结束 SSH 会话可以执行 `exit`；关闭网页不保证已启动的远程进程全部终止，长任务请使用 tmux 等明确管理。

证书是测试证书，浏览器可能提示不受信任。先通过可信 SSH 会话核对指纹，再按组织策略导入信任，
不要全局关闭浏览器证书校验。当前证书 SHA256 指纹：

```text
E3:3D:3C:22:90:1C:87:9D:A9:4E:BA:32:93:00:DC:3E:F9:8E:52:CF:8E:A9:A3:EB:C3:62:F0:69:03:91:AD:E4
```

验证命令：

```bash
sudo openssl x509 -in /opt/c-check-terminal/state/tls.crt -noout -fingerprint -sha256 -dates
```

隧道左侧端口可修改，但相应浏览器 URL 与预览目录中的 `terminal-config.json` 也要一起修改。
隧道必须在访问网页的电脑上建立；其他电脑的 localhost 不指向这台服务器。

## 5. 配置 C-Check 入口

前端启动后读取静态资源 `/terminal-config.json`，文件内容如下：

```json
{"url":"https://terminal.example.com/guacamole/"}
```

非签名的独立前端可以修改发布目录配置。对于 rc.20 签名部署，必须在包外创建该文件，
并通过站点配置 `web.terminal_config` 指定绝对路径，再由 rc.20 部署器 update 添加只读挂载。
详见包内 INSTALL-RTX5880.md。不要修改签名包文件或手改受管理 Compose，然后刷新页面。
无需为修改终端 URL 重新编译前端。前端镜像自身需要包含本次菜单代码，**现有 RC19 不会自动获得新菜单**。
源码默认 URL 为空，管理员看到禁用入口；普通 C-Check 用户不显示入口。
仅接受无用户名、无密码、无查询参数、无 fragment 的绝对 HTTPS URL，拒绝 HTTP/javascript 等地址。
新窗口使用 noopener/noreferrer，不向终端转发 C-Check JWT 或密码。
前端隐藏入口不是安全边界，真实权限由 Guacamole 独立认证和连接 ACL 决定。

生产环境建议使用独立域名与可信 CA 证书；先配置证书、VPN/访问源限制、账号权限、日志保留策略，
再评估是否调整 loopback 绑定。不要把测试预览、数据库、guacd 或 Tomcat 端口直接开放公网。
建议轮换本次测试所用的共享 SSH 密码，为运维人员分配各自的 Linux 与 Guacamole 账号。

## 6. 新增 SSH 连接和权限

准备 JSON 配置，`host-key` 填写**经独立可信渠道核对**的 SSH 公钥类型和 Base64 值，不含尾部注释。
脚本会补齐 known_hosts 所需的主机名及非标准端口格式。

```json
{
  "name": "Linux server",
  "hostname": "10.0.0.10",
  "port": 22,
  "username": "operator",
  "host-key": "ecdsa-sha2-nistp256 REPLACE_WITH_VERIFIED_PUBLIC_KEY"
}
```

```bash
sudo python3 /opt/c-check-terminal/terminal.py add-ssh /path/to/connection.json
```

不接受 password/private-key 字段；默认不启用 SFTP。重复连接名拒绝新增，已有连接在管理界面修改。
本机连接使用 `host.docker.internal:22`，不要填容器内的 `127.0.0.1`。
官方 guacd 在此主机上协商的是 ECDSA，而原生 SSH 客户端可能选择 Ed25519；
若提示主机密钥不匹配，应核实协商算法和对应公钥，不能直接去掉 host-key 绕过检查。
可信 SSH 会话可读取 `/etc/ssh/ssh_host_ecdsa_key.pub`；不要上传任何 `/etc/ssh/*_key` 私钥。

管理员在 Guacamole“设置”中创建普通终端用户，只授予指定连接的 READ 权限，不授予 ADMINISTER 或 CREATE_CONNECTION。
Guacamole 网页账号与 Linux SSH 账号是两层独立身份。数据库保存的是网页账号密码哈希；
若管理员以后在界面保存目标 SSH 密码/私钥，则这些连接参数进入数据库，备份必须作为敏感数据保护。

## 7. 启停、排错和验证

```bash
sudo python3 /opt/c-check-terminal/terminal.py status
sudo python3 /opt/c-check-terminal/terminal.py stop
sudo python3 /opt/c-check-terminal/terminal.py start
sudo python3 /opt/c-check-terminal/terminal.py check
sudo python3 /opt/c-check-terminal/terminal.py logs
```

这些命令只操作 `ccheck-terminal`。stop 保留数据卷、密码和证书，start 不重新初始化数据库。
不要用 `docker system prune`、全局停止所有容器或 `down -v` 排错。
HTTPS 就绪检查会验证测试证书，但 HTTP 200 本身不等于 SSH 可用，仍需登录并执行实际命令验证。

自动验证脚本会创建并清理一个随机的临时普通账号，验证默认口令失效、匿名拒绝、连接 ACL 和创建连接权限：

```bash
sudo python3 /opt/c-check-terminal/verify.py
cd /opt/c-check-terminal
python3 -m unittest -v test_terminal.py
```

verify.py 使用本机初始管理员凭据文件；管理员改密后先通过可信方式同步测试凭据，不能重新 init 重置数据库。
登录失败五次可触发临时封禁；SSH 隧道用户可能共用代理出口，不要暴力重试。
guacd、Guacamole、Nginx 使用非 root、只读根文件系统和 tmpfs；PostgreSQL 使用非 root 与持久卷。
所有容器 drop ALL capabilities、no-new-privileges，并有资源上限。
当前记录连接/登录事件，不启用键盘录制或完整会话录像，避免无意收集密码和业务数据。

## 8. 备份、恢复与升级

```bash
sudo python3 /opt/c-check-terminal/terminal.py backup /secure-backup/terminal.sql.gz
```

该命令只导出数据库；同时备份 `state`、镜像锁、Compose、Nginx 配置。
备份目录应仅管理员可读，建议加密并离机保存；不要只复制运行中的 PostgreSQL 卷文件。
state 内的 admin-credentials 是初始化记录，不会自动同步网页改密。

恢复时先在隔离机器演练：保留原实例；复制相同版本部署文件和 state，不再执行 init；
启动 database 服务到一个确认可丢弃的新项目卷，再用 psql 导入导出文件；检查管理员、连接、权限和实际 SSH。
不要把 SQL 导入现有业务库，不要未经备份替换当前项目卷。恢复和大版本数据库升级需单独制定迁移步骤。
本次已完成备份导出和启停持久化测试，未执行跨机器灾备恢复。

## 9. CPU 测试前端的复现

在仓库 frontend 目录执行 `npm ci`，以 `VITE_USE_MOCK_API=true npm run build` 构建测试预览。
将 dist 内容放入 `/opt/c-check-terminal/state/preview`，设置该目录和静态文件供 Nginx UID 101 读取，
但保留 state 父目录 0700；通过挂载子目录让容器访问，不放宽 state。
设置 preview/terminal-config.json 为本机隧道终端地址，然后：

```bash
cd /opt/c-check-terminal
sudo docker compose -p ccheck-terminal-preview --env-file state/compose.env -f compose.preview.yaml up -d
sudo docker compose -p ccheck-terminal-preview --env-file state/compose.env -f compose.preview.yaml stop
```

这是独立的前端测试项目，停止它应不影响终端访问。
正式 C-Check 前端构建必须使用真实 API 配置，不应把模拟数据预览当成生产部署。

## 10. 上游参考

- https://guacamole.apache.org/doc/gug/guacamole-docker.html
- https://guacamole.apache.org/doc/gug/reverse-proxy.html
- https://guacamole.apache.org/doc/gug/configuring-guacamole.html#ssh
- https://guacamole.apache.org/doc/gug/postgresql-auth.html
