# 简易版中转助手（relay-lite）

只有 **一键中转** 一个功能的 s-ui 小工具（完整版中转助手的简易版），基于 [中转助手 relay-helper](https://github.com/katon4612-spec/relay-helper) 精简而来。

粘贴落地 → 自动在 s-ui 里建好 **落地出站 + VLESS-Reality 入站 + 用户 + 路由规则** → 拿到客户链接和二维码。

「线路管理」页列出在这里建过的线路，可以复制链接、看二维码。

## 功能

- 登录：用 s-ui 面板的账号密码（s-ui 改密码后自动跟着变）；s-ui 连不上时账号留空、用备用密码
- 落地格式：`socks5://账号:密码@IP:端口`、`IP:端口:账号:密码`、`http(s)://…`、`vless:// vmess:// trojan:// hy2:// ss:// tuic:// anytls://`，一行一条；前面可以加名字：`我的线路A socks5://…`
- 客户节点统一用 VLESS-Reality，每条落地一个节点
- 节点名自动生成「落地国家 + 客户名 + 落地IP」，预览时可改
- 创建前可「检测落地」通不通
- 不设到期时间，节点永久有效（不用了在 s-ui 面板里删除）
- 结果：复制全部链接、单个/全部二维码下载（压缩包）
- 线路管理：只列出本工具建过的线路（s-ui 里手动建的不显示），支持搜索、复制链接（可多选）、二维码
- 安全：每个节点自动加兜底拒绝规则（用户停用/过期后不会从中转机直接出网）

和完整版相比 **没有**：Hysteria2 / SOCKS5 / 443 共用入站、到期时间、删除/续期/停用/换落地等线路操作、落地库、自动换落地、备用落地、双中转、订阅链接、流量/设备限额、备份恢复、多服务器、在线更新、Telegram 通知。

## 安装（一行命令）

在中转服务器上用 root 执行。有域名的，最后换成这台服务器的域名（先把域名解析到这台服务器）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/katon4612-spec/relay-lite/main/install.sh) hk2.liveshop.work
```

脚本会自动完成：

- 没装 s-ui 的：自动装 s-ui **1.3.10**，管理员账号 `admin` + 随机密码（装完显示在屏幕上，也是 s-ui 面板 `http://域名:2095/app/` 的密码）
- s-ui 里没有 Reality 配置的：自动生成一个（SNI `www.apple.com`）
- 装简易版中转助手、配 HTTPS 证书、设成开机自启

**没有域名**：后面什么都不写，自动用服务器的公网 IP（客户链接里也是 IP）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/katon4612-spec/relay-lite/main/install.sh)
```

用 IP 时页面是自签名证书，浏览器第一次打开提示「不安全」，点「高级 → 继续访问」即可（传输照样加密，节点不受影响）。

已经装了 s-ui 的服务器也用同一行命令，会直接沿用现有的 s-ui 和账号密码。装完屏幕上会显示访问地址（默认端口 **2098**）、账号密码和备用密码。

云服务商安全组需要放行：

| 端口 | 协议 | 用途 |
|---|---|---|
| 2098 | TCP | 简易版中转助手页面 |
| 10000-60000 | TCP | 客户节点端口 |
| 80 | TCP | 申请正规 HTTPS 证书时用 |

可以和完整版中转助手装在同一台服务器上（目录、服务名、端口都不同；同域名的证书会直接沿用完整版的）。

## 常用命令

| 要做什么 | 命令 |
|---|---|
| 升级 | 再执行一遍上面的安装命令（配置和线路都保留） |
| 改成好记的访问路径 | `sed -i 's#"path": *"[^"]*"#"path": "/kaide/"#' /opt/relay-lite/config.json && systemctl restart relay-lite` |
| 查看备用密码 | `grep '"password"' /opt/relay-lite/config.json` |
| 查看证书类型 | `grep https_mode /opt/relay-lite/config.json` |
| 重新配置 HTTPS | `bash /opt/relay-lite/https.sh /opt/relay-lite force && systemctl restart relay-lite` |
| 重启 | `systemctl restart relay-lite` |
| 查看日志 | `journalctl -u relay-lite -n 50` |
| 卸载（节点不受影响） | `bash <(curl -fsSL https://raw.githubusercontent.com/katon4612-spec/relay-lite/main/install.sh) uninstall` |

## 说明

- 只依赖 Python 3 标准库，通过 s-ui 自带的 API 操作，不改 s-ui 程序。
- 安装位置 `/opt/relay-lite`，配置 `config.json`，建过的线路记在 `lines.json`，服务名 `relay-lite`。
- 每次创建完会重启一次 s-ui 让路由生效（s-ui 1.3.10 进程内重启内核不可靠），所有线路约断 3 秒。
- 节点端口会加入 `ip_local_reserved_ports`，避免重启 s-ui 时端口被临时占用导致内核起不来。
