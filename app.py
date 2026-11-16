#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简易版中转助手 (relay-lite) —— 只有「一键中转」

粘贴落地（socks5 / http / vless / vmess / trojan / hy2 / ss / tuic / anytls 链接，或 IP:端口:账号:密码）
→ 在 s-ui 里自动建好：出站(落地) + 入站(VLESS-Reality) + 用户 + 路由规则
→ 输出客户链接和二维码。

基于中转助手 relay-helper 的一键中转部分精简而来。只依赖 Python 3 标准库，通过 s-ui 1.3.10 自带的 API 操作。
建好的节点之后在 s-ui 面板里管理（续期、停用、删除等）。
"""
import base64
import gzip
import hashlib
import hmac
import http.cookiejar
import ipaddress
import json
import os
import random
import re
import secrets
import shutil
import socket
import ssl
import string
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import qrencoder  # noqa: E402  (vendored, MIT)

CONFIG_PATH = os.environ.get("RELAY_LITE_CONFIG", os.path.join(HERE, "config.json"))
VERSION = "1.1.0"

DEFAULTS = {
    "listen": "0.0.0.0",
    "port": 2098,
    "path": "/lite/",
    "password": "",
    "sui_url": "http://127.0.0.1:2095/app/",
    "sui_user": "admin",
    "sui_pass": "",
    "sui_host": "",
    "public_host": "",
    "port_min": 10000,
    "port_max": 60000,
    "check_url": "https://www.gstatic.com/generate_204",
    "geo_lookup": True,
    "auto_firewall": True,
    "reserve_ports": True,
    "sui_restart_cmd": "systemctl restart s-ui",
    "cert_file": "",
    "key_file": "",
}


def load_config():
    cfg = dict(DEFAULTS)
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        cfg.update(json.load(f))
    if not cfg["path"].startswith("/"):
        cfg["path"] = "/" + cfg["path"]
    if not cfg["path"].endswith("/"):
        cfg["path"] += "/"
    if not cfg["sui_url"].endswith("/"):
        cfg["sui_url"] += "/"
    return cfg


def save_config(updates):
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        cur = json.load(f)
    cur.update(updates)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)
    CFG.update(updates)


CFG = None
LOCK = threading.Lock()  # 所有写 s-ui 的操作排队执行


# ---------------------------------------------------------------- s-ui API --
class SuiError(Exception):
    pass


class Sui:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg["sui_url"] + "api/"
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar), urllib.request.ProxyHandler({})]
        if cfg["sui_url"].startswith("https://"):
            # s-ui 面板开了 HTTPS 时，本机回环访问证书域名对不上，这里只对本机连接跳过校验
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        self.opener = urllib.request.build_opener(*handlers)
        # s-ui 设置了「面板域名」时会拒绝 Host 不匹配的请求；同时 s-ui 用 Host 生成客户链接里的地址
        self.host = cfg.get("sui_host") or cfg["public_host"] or None
        self.lock = threading.Lock()

    def _raw(self, method, action, params=None, form=None, timeout=30):
        url = self.base + action
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Requested-With", "XMLHttpRequest")
        if self.host:
            req.add_header("Host", self.host)
        with self.opener.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            raise SuiError("s-ui 返回了非 JSON 内容：" + body[:200])

    def login(self):
        try:
            res = self._raw("POST", "login", form={"user": self.cfg["sui_user"], "pass": self.cfg["sui_pass"]})
        except urllib.error.URLError as e:
            raise SuiError("连接 s-ui 失败：%s（检查 sui_url 配置）" % e)
        if not res.get("success"):
            raise SuiError("登录 s-ui 失败：" + str(res.get("msg")))

    def call(self, method, action, params=None, form=None, timeout=30):
        for attempt in range(2):
            try:
                res = self._raw(method, action, params, form, timeout)
            except urllib.error.URLError as e:
                raise SuiError("连接 s-ui 失败：%s（检查 sui_url 配置）" % e)
            if not res.get("success") and "Invalid login" in str(res.get("msg")) and attempt == 0:
                with self.lock:
                    self.login()
                continue
            if not res.get("success"):
                raise SuiError(str(res.get("msg")))
            return res.get("obj")
        raise SuiError("s-ui 登录失效")

    def get(self, what, **params):
        return self.call("GET", what, params=params or None)

    def save(self, obj, act, data, init_users=None):
        # s-ui 对所有 data 都做 json.Unmarshal，字符串（如要删除的 tag）也必须是 JSON 字符串
        form = {"object": obj, "action": act, "data": json.dumps(data, ensure_ascii=False)}
        if init_users is not None:
            form["initUsers"] = init_users
        return self.call("POST", "save", form=form)

    def load_all(self):
        return self.call("GET", "load")

    def link_convert(self, link):
        return self.call("POST", "linkConvert", form={"link": link})

    def check_outbound(self, tag, link):
        return self.call("GET", "checkOutbound", params={"tag": tag, "link": link}, timeout=40)


SUI = None


# -------------------------------------------------------------- utilities --
ALNUM = string.ascii_letters + string.digits


def rand_pw(n=10):
    return "".join(secrets.choice(ALNUM) for _ in range(n))


def b64key(n):
    return base64.b64encode(secrets.token_bytes(n)).decode()


def client_config(name):
    """和 s-ui 面板新建用户时生成的结构一样。"""
    u = str(uuid.uuid4())
    p = rand_pw()
    ss = b64key(32)
    return {
        "mixed": {"username": name, "password": p},
        "socks": {"username": name, "password": p},
        "http": {"username": name, "password": p},
        "shadowsocks": {"name": name, "password": ss},
        "shadowsocks16": {"name": name, "password": b64key(16)},
        "shadowtls": {"name": name, "password": ss},
        "vmess": {"name": name, "uuid": u, "alterId": 0},
        "vless": {"name": name, "uuid": u, "flow": "xtls-rprx-vision"},
        "anytls": {"name": name, "password": p},
        "trojan": {"name": name, "password": p},
        "naive": {"username": name, "password": p},
        "hysteria": {"name": name, "auth_str": p},
        "tuic": {"name": name, "uuid": u, "password": p},
        "hysteria2": {"name": name, "password": p},
    }


def is_ip(s):
    try:
        ipaddress.ip_address(s.strip("[]"))
        return True
    except ValueError:
        return False


def resolve(host):
    if is_ip(host):
        return host.strip("[]")
    try:
        return socket.gethostbyname(host)
    except OSError:
        return ""


SOCKS_RE = re.compile(r"^(socks5h?|socks4a?|socks|http|https)://(?:([^:@/]*)(?::([^@/]*))?@)?\[?([^\]/:@]+|[0-9a-fA-F:]+)\]?:(\d+)/?(?:#(.*))?$", re.I)


def parse_landing(raw):
    """返回 (出站 dict（不含 tag）, 落地地址, 备注)，格式不对抛 ValueError。"""
    s = raw.strip()
    if not s:
        raise ValueError("空行")
    m = SOCKS_RE.match(s)
    if m:
        scheme, user, pw, host, port, remark = m.groups()
        scheme = scheme.lower()
        user = urllib.parse.unquote(user or "")
        pw = urllib.parse.unquote(pw or "")
        if scheme.startswith("socks") and user and not pw:
            # socks5://base64(user:pass)@host:port  (v2rayN 格式)
            try:
                dec = base64.b64decode(user + "=" * (-len(user) % 4)).decode()
                if ":" in dec:
                    user, pw = dec.split(":", 1)
            except Exception:
                pass
        if scheme.startswith("socks"):
            ob = {"type": "socks", "server": host, "server_port": int(port), "version": "4" if scheme.startswith("socks4") else "5"}
        else:
            ob = {"type": "http", "server": host, "server_port": int(port)}
            if scheme == "https":
                ob["tls"] = {"enabled": True, "server_name": host}
        if user:
            ob["username"] = user
            ob["password"] = pw
        return ob, host, urllib.parse.unquote(remark or "")
    if "://" in s:
        scheme = s.split("://", 1)[0].lower()
        if scheme not in ("vless", "vmess", "trojan", "hy", "hysteria", "hy2", "hysteria2", "ss", "shadowsocks", "tuic", "anytls"):
            raise ValueError("不支持的链接类型：" + scheme)
        ob = SUI.link_convert(s)
        if not isinstance(ob, dict) or not ob.get("type"):
            raise ValueError("链接解析失败")
        ob = dict(ob)
        remark = ob.pop("tag", "") or ""
        if "#" in s:
            remark = urllib.parse.unquote(s.rsplit("#", 1)[1])
        return ob, ob.get("server", ""), remark
    # IP:端口:账号:密码  /  IP:端口  /  IP 端口 账号 密码
    parts = [p for p in re.split(r"[:\s|,，]+", s) if p]
    if len(parts) >= 2 and parts[1].isdigit():
        host, port = parts[0], int(parts[1])
        ob = {"type": "socks", "server": host, "server_port": port, "version": "5"}
        if len(parts) >= 4:
            ob["username"] = parts[2]
            ob["password"] = ":".join(parts[3:])
        elif len(parts) == 3:
            raise ValueError("格式应为 IP:端口:账号:密码")
        return ob, host, ""
    raise ValueError("无法识别的格式")


GEO_CACHE = {}


def geo_lookup(ips):
    """ip -> 国家中文名（ip-api.com 免费批量接口，每次最多 100 个），只用来给节点起名。"""
    out, todo = {}, []
    for ip in ips:
        if not ip:
            continue
        if ip in GEO_CACHE:
            out[ip] = GEO_CACHE[ip]
        elif ip not in todo:
            todo.append(ip)
    if not (todo and CFG.get("geo_lookup", True)):
        return out
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        body = json.dumps([{"query": ip, "fields": "status,country,query", "lang": "zh-CN"} for ip in chunk]).encode()
        req = urllib.request.Request("http://ip-api.com/batch", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                for item in json.loads(r.read().decode()):
                    if item.get("status") == "success" and item.get("country"):
                        GEO_CACHE[item["query"]] = item["country"]
                        out[item["query"]] = item["country"]
        except Exception:
            break   # 查不到国家不影响创建，名字里少个国家而已
    return out


def used_ports(data):
    ports = set()
    for inb in data.get("inbounds") or []:
        try:
            ports.add(int(inb.get("listen_port")))
        except (TypeError, ValueError):
            pass
    return ports


def port_free(p, need_udp):
    for kind in ([socket.SOCK_STREAM, socket.SOCK_DGRAM] if need_udp else [socket.SOCK_STREAM]):
        for fam, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            try:
                s = socket.socket(fam, kind)
            except OSError:
                continue
            try:
                s.bind((addr, p))
            except OSError:
                return False
            finally:
                s.close()
    return True


def pick_port(taken, need_udp):
    lo, hi = int(CFG["port_min"]), int(CFG["port_max"])
    for _ in range(500):
        p = random.randint(lo, hi)
        if p in taken:
            continue
        if port_free(p, need_udp):
            taken.add(p)
            return p
    raise SuiError("找不到可用端口")


def open_firewall(port, udp):
    if not CFG.get("auto_firewall", True):
        return ""
    protos = ["tcp"] + (["udp"] if udp else [])
    try:
        if shutil.which("ufw"):
            st = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=10).stdout
            if "Status: active" in st or "状态： 激活" in st:
                for pr in protos:
                    subprocess.run(["ufw", "allow", "%d/%s" % (port, pr)], capture_output=True, timeout=10)
                return "ufw"
        if shutil.which("firewall-cmd"):
            st = subprocess.run(["firewall-cmd", "--state"], capture_output=True, text=True, timeout=10).stdout
            if "running" in st:
                for pr in protos:
                    subprocess.run(["firewall-cmd", "--permanent", "--add-port=%d/%s" % (port, pr)], capture_output=True, timeout=10)
                subprocess.run(["firewall-cmd", "--reload"], capture_output=True, timeout=20)
                return "firewalld"
    except Exception:
        pass
    return ""


def qr_svg(text, scale=6, border=4):
    q = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    q.addData(text)
    q.make()
    n = q.getModuleCount()
    size = (n + 2 * border) * scale
    path = []
    for r in range(n):
        for c in range(n):
            if q.isDark(r, c):
                path.append("M%d %dh%dv%dh-%dz" % ((c + border) * scale, (r + border) * scale, scale, scale, scale))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" shape-rendering="crispEdges">'
            '<rect width="100%%" height="100%%" fill="#fff"/><path fill="#000" d="%s"/></svg>') % (size, size, size, size, "".join(path))


def find_tls(data, kind):
    """kind = 'reality' 或 'cert'，返回 s-ui 里对应 TLS 配置的 id，没有返回 0。"""
    for t in data.get("tls") or []:
        srv = t.get("server") or {}
        if isinstance(srv, str):
            try:
                srv = json.loads(srv)
            except ValueError:
                srv = {}
        is_reality = bool((srv.get("reality") or {}).get("enabled"))
        has_cert = bool(srv.get("certificate") or srv.get("certificate_path") or srv.get("key") or srv.get("key_path"))
        if kind == "reality" and is_reality:
            return t["id"]
        if kind == "cert" and not is_reality and has_cert:
            return t["id"]
    return 0


def unique_name(name, taken):
    n, k = name, 2
    while n in taken:
        n = "%s-%d" % (name, k)
        k += 1
    taken.add(n)
    return n


def core_running():
    try:
        st = SUI.call("GET", "status", params={"r": "sbd"}, timeout=5)
        return bool(((st or {}).get("sbd") or {}).get("running"))
    except Exception:
        return False


def ensure_core():
    """
    保存路由规则后调用。s-ui 1.3.10 在进程内重启 sing-box 不可靠（hysteria2 的 UDP 端口经常没释放，新内核起不来），
    所以直接重启整个 s-ui 服务（约 3 秒），内核没起来就再重启，最多 3 次。
    返回 "restarted" | "ok" | "down"
    """
    reserve_ports()
    cmd = CFG.get("sui_restart_cmd") or ""
    if not cmd:
        deadline = time.time() + 8
        while time.time() < deadline:
            if core_running():
                return "ok"
            time.sleep(0.7)
        return "down"
    time.sleep(0.5)
    for attempt in range(3):
        try:
            subprocess.run(cmd, shell=True, timeout=60, capture_output=True)
        except Exception:
            pass
        deadline = time.time() + 20
        time.sleep(2)
        while time.time() < deadline:
            if core_running():
                return "restarted"
            time.sleep(1)
        sys.stderr.write("s-ui 内核未启动，第 %d 次重启后重试\n" % (attempt + 1))
    return "down"


RESERVED_FILE = "/etc/sysctl.d/99-relay-lite.conf"


def reserve_ports():
    """
    把所有入站端口加入 net.ipv4.ip_local_reserved_ports：系统给往外的连接分配本地端口时会避开它们，
    防止重启 s-ui 那一刻节点端口正好被占、内核启动失败。只影响自动分配，不影响程序主动监听。
    """
    if not CFG.get("reserve_ports", True):
        return
    try:
        data = SUI.load_all()
        ports = sorted({int(i.get("listen_port")) for i in data.get("inbounds") or [] if str(i.get("listen_port") or "").isdigit()})
        path = "/proc/sys/net/ipv4/ip_local_reserved_ports"
        if not ports or not os.access(path, os.W_OK):
            return
        cur = open(path).read().strip()
        mine_old = ""
        if os.path.exists(RESERVED_FILE):
            for line in open(RESERVED_FILE):
                if line.startswith("net.ipv4.ip_local_reserved_ports"):
                    mine_old = line.split("=", 1)[1].strip()

        def expand(s):
            out = set()
            for part in [x for x in s.replace(" ", "").split(",") if x]:
                a, _, b = part.partition("-")
                if a.isdigit():
                    out.update(range(int(a), int(b or a) + 1))
            return out
        allp = sorted((expand(cur) - expand(mine_old)) | set(ports))   # 保留别人设的，替换自己上次写的
        ranges, start, prev = [], None, None
        for p in allp:
            if start is None:
                start = prev = p
            elif p == prev + 1:
                prev = p
            else:
                ranges.append("%d-%d" % (start, prev) if prev > start else str(start))
                start = prev = p
        if start is not None:
            ranges.append("%d-%d" % (start, prev) if prev > start else str(start))
        with open(path, "w") as f:
            f.write(",".join(ranges))
        with open(RESERVED_FILE, "w") as f:
            f.write("# 由简易版中转助手维护：s-ui 入站端口，避免被系统分配给往外的连接\n"
                    "net.ipv4.ip_local_reserved_ports = %s\n" % ",".join(str(p) for p in ports))
    except Exception as e:
        sys.stderr.write("reserve_ports: %s\n" % e)


# ----------------------------------------------------------- 一键中转 --
LINES_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "lines.json")
LINES = {}   # 本工具建过的线路：节点名 -> 创建时间（线路管理只列这些）
LINES_LOCK = threading.Lock()
MARK = "简易版中转助手创建"


def load_lines():
    try:
        with open(LINES_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict):
            LINES.update(d)
    except Exception:
        pass


def save_lines():
    with LINES_LOCK:
        tmp = LINES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(LINES, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, LINES_PATH)


def op_preview(body):
    """解析粘贴的落地，生成节点名（落地国家 + 客户名 + 落地 IP）。"""
    lines = [l for l in (body.get("text") or "").splitlines() if l.strip()]
    if not lines:
        raise SuiError("请先粘贴落地信息")
    if len(lines) > 300:
        raise SuiError("一次最多 300 条")
    customer = (body.get("customer") or "").strip()
    data = SUI.load_all()
    taken = {i.get("tag") for i in data.get("inbounds") or []} | {o.get("tag") for o in data.get("outbounds") or []} | {c.get("name") for c in data.get("clients") or []}
    items = []
    for ln in lines:
        name_override = ""
        raw = ln.strip()
        # 支持「名称 空格 链接」
        m = re.match(r"^(\S+)\s+((?:[a-zA-Z0-9]+://)\S+)$", raw)
        if m and not m.group(1).count("://"):
            name_override, raw = m.group(1), m.group(2)
        try:
            ob, host, _ = parse_landing(raw)
            items.append({"raw": raw, "outbound": ob, "host": host, "ip": resolve(host), "name_override": name_override, "error": ""})
        except Exception as e:
            items.append({"raw": raw, "error": str(e)})
    geo = geo_lookup([it.get("ip") for it in items if not it.get("error")])
    for it in items:
        if it.get("error"):
            continue
        it["country"] = geo.get(it["ip"], "")
        it["name"] = unique_name(it.pop("name_override") or ("%s%s%s" % (it["country"], customer, it["host"])), taken)
    return {"items": items}


def op_check_raw(body):
    """创建前检测落地：临时建出站 → 用 s-ui 测一下 → 删掉。"""
    items = [it for it in body.get("items") or [] if not it.get("error")]
    url = CFG["check_url"]
    tmp = []
    with LOCK:
        for it in items:
            t = "__lite_check_%s" % secrets.token_hex(4)
            ob = dict(it["outbound"])
            ob["tag"] = t
            try:
                SUI.save("outbounds", "new", ob)
                tmp.append((it.get("raw"), t, ""))
            except Exception as e:
                tmp.append((it.get("raw"), None, str(e)))
        try:
            def one(x):
                if x[1] is None:
                    return {"raw": x[0], "ok": False, "error": x[2]}
                try:
                    r = SUI.check_outbound(x[1], url) or {}
                    return {"raw": x[0], "ok": bool(r.get("OK")), "delay": r.get("Delay"), "error": r.get("Error") or ""}
                except Exception as e:
                    return {"raw": x[0], "ok": False, "error": str(e)}
            with ThreadPoolExecutor(max_workers=8) as ex:
                res = list(ex.map(one, tmp))
        finally:
            for x in tmp:
                if x[1]:
                    try:
                        SUI.save("outbounds", "del", x[1])
                    except Exception:
                        pass
    return {"results": res}


def _prepare(data):
    """检查 Reality 配置，返回 tls_id，以及已占用的名字和端口。"""
    tls_id = find_tls(data, "reality")
    if not tls_id:
        raise SuiError("s-ui 里没有 Reality 配置。请先在面板「TLS 设置」里添加一个 reality 类型的配置")
    taken_names = {i.get("tag") for i in data.get("inbounds") or []} | {o.get("tag") for o in data.get("outbounds") or []} | {c.get("name") for c in data.get("clients") or []}
    taken_ports = used_ports(data)
    settings = SUI.get("settings") or {}
    for k in ("webPort", "subPort"):
        try:
            taken_ports.add(int(settings.get(k)))
        except (TypeError, ValueError):
            pass
    taken_ports.add(int(CFG["port"]))
    return tls_id, taken_names, taken_ports


def _make_inbound(name, tls_id, expiry, group, taken_ports, outbound_tag):
    """建一个 VLESS-Reality 入站 + 用户。成功返回结果（含要加的路由规则）；失败时回滚自己建的部分并抛异常。"""
    created = {}
    try:
        port = pick_port(taken_ports, False)
        inb = {"type": "vless", "tag": name, "listen": "::", "listen_port": port, "tls_id": tls_id, "addrs": [],
               "out_json": {}, "transport": {}}
        res = SUI.save("inbounds", "new", inb, init_users="")
        inb_id = next((i["id"] for i in (res or {}).get("inbounds") or [] if i.get("tag") == name), None)
        if not inb_id:
            raise SuiError("入站创建后未找到 id")
        created["inbound"] = name
        client = {"enable": True, "name": name, "config": client_config(name), "inbounds": [inb_id],
                  "links": [], "volume": 0, "expiry": int(expiry or 0), "up": 0, "down": 0,
                  "desc": MARK, "group": group or ""}
        res = SUI.save("clients", "new", client)
        cid = next((c["id"] for c in (res or {}).get("clients") or [] if c.get("name") == name), None)
        created["client"] = cid
        fw = open_firewall(port, False)
        return {"name": name, "protocol": "vless", "port": port, "client_id": cid, "ok": True, "firewall": fw,
                "outbound": outbound_tag,
                # 第二条是兜底：用户停用/过期后，没匹配上落地规则的连接一律拒绝，不会从中转机直接出网
                "rules": [{"inbound": [name], "auth_user": [name], "action": "route", "outbound": outbound_tag},
                          {"inbound": [name], "action": "reject"}]}
    except Exception:
        _rollback({"client_id": created.get("client"), "name": name if created.get("inbound") else None})
        raise


def _rollback(r, outbound=None):
    for obj, key in (("clients", r.get("client_id")), ("inbounds", r.get("name")), ("outbounds", outbound)):
        try:
            if key:
                SUI.save(obj, "del", key)
        except Exception:
            pass


def _apply_rules(results, new_outbounds):
    """一次性写入这批路由规则并重启 s-ui；失败则整批回滚。返回内核状态。"""
    new_rules = [x for r in results if r.get("ok") and "rules" in r for x in r.pop("rules")]
    if not new_rules:
        return "ok"
    # 兜底拒绝规则排在所有用户规则后面
    rej = lambda x: x.get("action") == "reject" and not x.get("auth_user")
    new_rules = [x for x in new_rules if not rej(x)] + [x for x in new_rules if rej(x)]
    cfg = SUI.get("config")
    cfg = cfg.get("config", cfg) if isinstance(cfg, dict) and isinstance(cfg.get("config"), dict) else cfg
    rules = cfg.setdefault("route", {}).setdefault("rules", [])
    pos = 0
    while pos < len(rules) and rules[pos].get("action") in ("sniff", "hijack-dns"):
        pos += 1
    rules[pos:pos] = new_rules
    try:
        SUI.save("config", "edit", cfg)
    except Exception as e:
        # 路由没保存成功：回滚这一批，避免留下没有路由（会从中转机直出）的入站
        for r in results:
            if r.get("ok"):
                _rollback(r)
                r.update(ok=False, error="保存路由失败，已回滚：%s" % e)
        for ob in new_outbounds:
            _rollback({}, ob)
        return "ok"
    return ensure_core()


def client_links(cid):
    c = SUI.get("clients", id=str(cid))
    if isinstance(c, dict):
        c = c.get("clients", c)
    if isinstance(c, list):
        c = c[0] if c else {}
    links = c.get("links") or []
    if isinstance(links, str):
        links = json.loads(links)
    return [l.get("uri") for l in links if l.get("uri")]


def op_create(body):
    items = [it for it in body.get("items") or [] if not it.get("error") and it.get("name")]
    expiry = int(body.get("expiry") or 0)
    group = (body.get("customer") or "").strip()
    if not items:
        raise SuiError("没有可创建的线路")
    with LOCK:
        data = SUI.load_all()
        tls_id, taken_names, taken_ports = _prepare(data)
        results, new_outbounds = [], []
        for idx, it in enumerate(items):
            name = unique_name(str(it["name"]).strip(), taken_names)
            ob = {k: v for k, v in dict(it["outbound"]).items() if k != "tag"}
            ob["tag"] = name
            try:
                SUI.save("outbounds", "new", ob)
            except Exception as e:
                results.append({"name": name, "ok": False, "error": "落地出站创建失败：%s" % e, "item": idx})
                continue
            try:
                r = _make_inbound(name, tls_id, expiry, group, taken_ports, name)
                new_outbounds.append(name)
            except Exception as e:
                _rollback({}, name)  # 入站没建成：落地出站也删掉
                r = {"name": name, "protocol": "vless", "ok": False, "error": str(e)}
            r["item"] = idx
            results.append(r)
        core = _apply_rules(results, new_outbounds)
        now = int(time.time())
        made = [r["name"] for r in results if r.get("ok")]
        if made:
            with LINES_LOCK:
                LINES.update({n: now for n in made})
            save_lines()
    for r in results:
        if r.get("ok") and r.get("client_id"):
            try:
                r["links"] = client_links(r["client_id"])
            except Exception as e:
                r["links"] = []
                sys.stderr.write("links: %s\n" % e)
    return {"results": results, "core": core}


def _rows(data):
    """本工具建过的线路：入站 + 用户 + 路由到的落地出站。"""
    outbounds = {o.get("tag"): o for o in data.get("outbounds") or []}
    rules = (((data.get("config") or {}).get("route") or {}).get("rules")) or []
    route_of = {}
    for r in rules:
        if r.get("action", "route") == "route" and r.get("outbound"):
            for t in r.get("inbound") or []:
                route_of.setdefault(t, r["outbound"])
    clients_by_inb = {}
    for c in data.get("clients") or []:
        ids = c.get("inbounds") or []
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except ValueError:
                ids = []
        for i in ids:
            clients_by_inb.setdefault(i, []).append(c)
    with LINES_LOCK:
        mine = dict(LINES)
    rows = []
    for inb in data.get("inbounds") or []:
        tag = inb.get("tag")
        cl = clients_by_inb.get(inb.get("id"), [])
        c = next((x for x in cl if x.get("name") == tag), cl[0] if cl else None)
        if tag not in mine and not (c and c.get("desc") == MARK):
            continue
        ot = route_of.get(tag)
        ob = outbounds.get(ot) or {}
        rows.append({
            "tag": tag, "port": inb.get("listen_port"), "inbound_id": inb.get("id"),
            "outbound_tag": ot, "landing": "%s:%s" % (ob.get("server"), ob.get("server_port")) if ob.get("server") else (ot or ""),
            "landing_type": ob.get("type") or "",
            "client_id": c.get("id") if c else None, "customer": (c or {}).get("group") or "",
            "enable": bool(c.get("enable")) if c else None, "expiry": (c or {}).get("expiry") or 0,
            "created": mine.get(tag, 0),
        })
    rows.sort(key=lambda r: (r["created"], r.get("inbound_id") or 0), reverse=True)
    return rows


def op_list(body):
    return {"lines": _rows(SUI.load_all()), "now": int(time.time())}


def op_links(body):
    out = []
    for cid in body.get("client_ids") or []:
        for u in client_links(cid):
            out.append({"client_id": cid, "uri": u, "svg": qr_svg(u)})
    return {"links": out}


def op_delete(body):
    """
    顺序：用户 → 入站 → 路由规则（只移除真正删掉的入站）→ 重启 s-ui → 落地出站。
    不会留下「入站还在但路由规则没了」的情况（那会让流量从中转机直接出网）。
    """
    tags = list(dict.fromkeys(body.get("tags") or []))
    if not tags:
        raise SuiError("请选择要删除的线路")
    out = []
    with LOCK:
        data = SUI.load_all()
        rows = {r["tag"]: r for r in _rows(data)}
        deleted = set()
        for t in tags:
            row = rows.get(t)
            if not row:
                out.append({"tag": t, "ok": False, "error": "不存在或不是这里建的线路"})
                continue
            try:
                if row.get("client_id"):
                    SUI.save("clients", "del", row["client_id"])
                SUI.save("inbounds", "del", t)
                deleted.add(t)
                out.append({"tag": t, "ok": True})
            except Exception as e:
                out.append({"tag": t, "ok": False, "error": str(e)})
        core = "ok"
        if deleted:
            cfg = SUI.get("config")
            cfg = cfg.get("config", cfg) if isinstance(cfg, dict) and isinstance(cfg.get("config"), dict) else cfg
            rules = (cfg.get("route") or {}).get("rules") or []
            newrules = []
            for r in rules:
                inb = r.get("inbound")
                if inb and any(x in deleted for x in inb):
                    keep = [x for x in inb if x not in deleted]
                    if not keep:
                        continue
                    r = dict(r)
                    r["inbound"] = keep
                    if r.get("auth_user"):
                        r["auth_user"] = [u for u in r["auth_user"] if u not in deleted]
                        if not r["auth_user"]:
                            r.pop("auth_user")
                newrules.append(r)
            if newrules != rules:
                cfg["route"]["rules"] = newrules
                SUI.save("config", "edit", cfg)
                core = ensure_core()
            still_used = {r.get("outbound") for r in newrules}
            for t in deleted:
                ot = rows[t].get("outbound_tag")
                if ot and ot not in still_used and ot != "direct":
                    try:
                        SUI.save("outbounds", "del", ot)
                    except Exception:
                        pass
            with LINES_LOCK:
                for t in deleted:
                    LINES.pop(t, None)
            save_lines()
    return {"results": out, "core": core}


def op_qr(body):
    return {"svg": qr_svg(body.get("text") or "")}


def op_status(body):
    data = SUI.load_all()
    return {"version": VERSION, "public_host": CFG["public_host"],
            "reality_tls": find_tls(data, "reality")}


OPS = {"status": op_status, "preview": op_preview, "check_raw": op_check_raw, "create": op_create, "qr": op_qr,
       "list": op_list, "links": op_links, "delete": op_delete}


# ------------------------------------------------------------- 登录 --
SESSIONS = {}
FAILS = {}
SESSION_AGE = 7 * 86400
SESS_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "sessions.json")
SESS_LOCK = threading.Lock()
LOGIN_ERR = {"detail": ""}


def load_sessions():
    try:
        with open(SESS_PATH) as f:
            d = json.load(f)
        now = time.time()
        SESSIONS.update({k: v for k, v in d.items() if isinstance(v, (int, float)) and v > now})
    except Exception:
        pass


def save_sessions():
    with SESS_LOCK:
        now = time.time()
        for k in [k for k, v in SESSIONS.items() if v < now]:
            SESSIONS.pop(k, None)
        try:
            tmp = SESS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(SESSIONS, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, SESS_PATH)
        except Exception as e:
            sys.stderr.write("save sessions: %s\n" % e)


def check_password(p):
    return bool(CFG["password"]) and hmac.compare_digest(hashlib.sha256((p or "").encode()).digest(),
                                                         hashlib.sha256(CFG["password"].encode()).digest())


def check_sui_login(user, pw):
    """用 s-ui 的账号密码登录（每次问 s-ui，s-ui 改了密码这里自动跟着变）。True / False；s-ui 连不上返回 None。"""
    if not user or not pw:
        return False
    LOGIN_ERR["detail"] = ""
    try:
        Sui(dict(CFG, sui_user=user, sui_pass=pw)).login()
    except SuiError as e:
        if "连接 s-ui 失败" in str(e):
            LOGIN_ERR["detail"] = str(e)
            return None
        return False
    except Exception as e:
        LOGIN_ERR["detail"] = "%s %s（地址 %s）" % (type(e).__name__, e, CFG.get("sui_url"))
        return None
    if user != CFG.get("sui_user") or pw != CFG.get("sui_pass"):
        try:   # s-ui 改过密码：顺便更新连 s-ui 用的账号
            save_config({"sui_user": user, "sui_pass": pw})
            globals()["SUI"] = Sui(CFG)
        except Exception as e:
            sys.stderr.write("update sui creds: %s\n" % e)
    return True


# ------------------------------------------------------------- web server --
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 75
    server_version = "relay-lite"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        gz = len(body) > 1400 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            body = gzip.compress(body, 5)
        if code >= 400:
            self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if code >= 400:
            self.send_header("Connection", "close")
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _session(self):
        m = re.search(r"rl_sess=([A-Za-z0-9_\-]+)", self.headers.get("Cookie") or "")
        exp = SESSIONS.get(m.group(1)) if m else None
        return bool(exp and exp > time.time())

    def setup(self):
        # HTTPS：TLS 握手放在处理线程里做，慢客户端不会卡住主线程
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(15)
            self.request.do_handshake()
            self.request.settimeout(None)
        super().setup()

    def do_GET(self):
        p = CFG["path"]
        path = self.path.split("?", 1)[0]
        if path == p.rstrip("/"):
            return self._send(302, "", headers={"Location": p})
        if path == p or path == p + "index.html":
            with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
                html = f.read().replace("__BASE__", p).replace("__VERSION__", VERSION)
            return self._send(200, html, "text/html; charset=utf-8")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        p = CFG["path"]
        path = self.path.split("?", 1)[0]
        if not path.startswith(p + "api/"):
            return self._send(404, {"ok": False, "error": "not found"})
        action = path[len(p + "api/"):]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 8 << 20:
                return self._send(413, {"ok": False, "error": "请求太大"})
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except ValueError:
            return self._send(400, {"ok": False, "error": "bad json"})
        if action == "login":
            ip = self.client_address[0]
            fl = [t for t in FAILS.get(ip, []) if t > time.time() - 600]
            if len(fl) >= 8:
                return self._send(429, {"ok": False, "error": "失败次数太多，10 分钟后再试"})
            ok = check_password(body.get("password"))
            sui_down = False
            if not ok and body.get("username"):
                r = check_sui_login(str(body.get("username", "")).strip(), body.get("password") or "")
                ok, sui_down = bool(r), r is None
            if not ok:
                fl.append(time.time())
                FAILS[ip] = fl
                time.sleep(1)
                return self._send(200, {"ok": False, "error": ("没能向 s-ui 验证账号（%s）。可以账号留空、用备用密码登录（grep password /opt/relay-lite/config.json）" % LOGIN_ERR["detail"])
                                        if sui_down else "账号或密码错误"})
            sid = secrets.token_urlsafe(32)
            SESSIONS[sid] = time.time() + SESSION_AGE
            save_sessions()
            secure = "; Secure" if CFG.get("cert_file") else ""
            return self._send(200, {"ok": True}, headers={
                "Set-Cookie": "rl_sess=%s; Path=%s; Max-Age=%d; HttpOnly; SameSite=Strict%s" % (sid, p, SESSION_AGE, secure)})
        if action == "logout":
            m = re.search(r"rl_sess=([A-Za-z0-9_\-]+)", self.headers.get("Cookie") or "")
            if m and SESSIONS.pop(m.group(1), None):
                save_sessions()
            return self._send(200, {"ok": True}, headers={"Set-Cookie": "rl_sess=; Path=%s; Max-Age=0" % p})
        if not self._session():
            return self._send(401, {"ok": False, "error": "请先登录", "login": True})
        fn = OPS.get(action)
        if not fn:
            return self._send(404, {"ok": False, "error": "unknown action"})
        try:
            res = fn(body)
            res["ok"] = res.get("ok", True)
            return self._send(200, res)
        except (SuiError, ValueError) as e:
            return self._send(200, {"ok": False, "error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(200, {"ok": False, "error": "内部错误：%s" % e})


def main():
    global CFG, SUI
    CFG = load_config()
    if not CFG["password"]:
        sys.exit("config.json 里必须设置 password")
    if not CFG["public_host"]:
        sys.exit("config.json 里必须设置 public_host（客户链接里用的域名或 IP）")
    SUI = Sui(CFG)
    try:
        SUI.login()
        print("已连接 s-ui：", CFG["sui_url"])
        reserve_ports()
    except Exception as e:
        print("警告：暂时无法登录 s-ui：", e)
    load_sessions()
    load_lines()
    srv = ThreadingHTTPServer((CFG["listen"], int(CFG["port"])), Handler)
    scheme = "http"
    if CFG.get("cert_file") and CFG.get("key_file"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CFG["cert_file"], CFG["key_file"])
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
        scheme = "https"

        def cert_watch():   # 证书续签后自动换上新证书，不用重启
            mt = lambda: tuple(os.path.getmtime(f) for f in (CFG["cert_file"], CFG["key_file"]))
            last = mt()
            while True:
                time.sleep(3600)
                try:
                    now = mt()
                    if now != last:
                        ctx.load_cert_chain(CFG["cert_file"], CFG["key_file"])
                        last = now
                except Exception as e:
                    sys.stderr.write("cert reload: %s\n" % e)
        threading.Thread(target=cert_watch, daemon=True).start()
    print("简易版中转助手 %s 已启动：%s://<服务器>:%s%s" % (VERSION, scheme, CFG["port"], CFG["path"]))
    srv.serve_forever()


if __name__ == "__main__":
    main()
