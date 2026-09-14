"""cyRouter CLI — 命令行控制（密钥管理等）。

用法:
    python cli.py key list                    # 列出密钥（掩码）
    python cli.py key create NAME [--copy]    # 创建密钥（--copy 顺便进剪贴板）
    python cli.py key reveal NAME             # 显示完整密钥
    python cli.py key revoke NAME             # 吊销密钥
    python cli.py config get KEY              # 读配置字段（如 port/host）
    python cli.py config set KEY VALUE        # 写配置字段
    python cli.py providers list              # provider 一览

    exe 场景同入口: cyRouter.exe key list（launcher.py 转发）

双模式:
    在线（服务运行中）: 走 HTTP /api/*——与 Web UI 同源同权限，无缓存一致性
        问题（服务有 30s 配置缓存，直改文件可能被运行中服务写回覆盖）。
    离线: 直接读写 config.json，改动在服务下次启动后生效（CLI 会提示）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# 探测顺序: config.json 里配置的端口 → dev 20100 → frozen 20108
_CANDIDATE_PORTS = (20100, 20108)
_TIMEOUT = 3


def _server_base() -> str | None:
    """探测本地服务（返回 http://127.0.0.1:PORT 或 None=离线）。"""
    cfg_port = None
    try:
        import config as _cfg

        cfg_port = int(_cfg.load_config().get("port", 0))
    except Exception:  # noqa: BLE001 — 探测失败按无配置处理
        pass
    ports = ([cfg_port] if cfg_port else []) + [p for p in _CANDIDATE_PORTS if p != cfg_port]
    for port in ports:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=_TIMEOUT
            ) as resp:
                if resp.status == 200:
                    return f"http://127.0.0.1:{port}"
        except (urllib.error.URLError, OSError, TimeoutError):
            continue
    return None


def _admin_token() -> str:
    """admin_token：env CYROUTER_ADMIN_TOKEN 优先，否则读本机 config.json。"""
    import os as _os_mod

    t = _os_mod.environ.get("CYROUTER_ADMIN_TOKEN", "")
    if t:
        return t
    try:
        import config as _cfg

        return str(_cfg.load_config().get("admin_token", ""))
    except Exception:  # noqa: BLE001 — 无配置环境按空处理
        return ""


def _http(base: str, method: str, path: str, body: dict | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    tok = _admin_token()
    if tok:
        headers["X-Admin-Token"] = tok
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT + 7) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        print(f"✗ HTTP {e.code}: {detail}", file=sys.stderr)
        sys.exit(1)


# ── key ────────────────────────────────────────────────────────────────


def cmd_token(args) -> None:
    """显示 admin_token（dashboard 首次访问 / 脚本集成用）。"""
    tok = _admin_token()
    if tok:
        print(tok)
    else:
        print("（未设置：服务启动后自动生成于 config.json，重跑本命令查看）")


def cmd_key(args) -> None:
    base = _server_base()
    sub = args.key_cmd

    if base:
        if sub == "list":
            data = _http(base, "GET", "/api/keys")
            keys = data.get("keys", [])
            if not keys:
                print("（无密钥）")
                return
            print(f"{'NAME':<20} {'KEY':<18} CREATED")
            for k in keys:
                print(f"{k['name']:<20} {k.get('key_masked', '***'):<18} {k.get('created_at', '')}")
            return
        if sub == "create":
            data = _http(base, "POST", "/api/keys", {"name": args.name})
            key = data["key"]
            print(f"✓ 已创建 '{args.name}': {key}")
            if args.copy:
                _to_clipboard(key)
                print("（已复制到剪贴板）")
            return
        if sub == "reveal":
            data = _http(base, "GET", f"/api/keys/{args.name}/reveal")
            print(data.get("key", ""))
            return
        if sub == "revoke":
            _http(base, "DELETE", f"/api/keys/{args.name}")
            print(f"✓ 已吊销 '{args.name}'")
            return

    # ── 离线: 直改 config.json ──
    import config as _cfg

    print("（服务未运行 → 离线直改 config.json，服务下次启动生效）")
    cfg = _cfg.load_config()
    api_keys = cfg.get("api_keys", [])
    if sub == "list":
        if not api_keys:
            print("（无密钥）")
            return
        print(f"{'NAME':<20} {'KEY':<18} CREATED")
        for k in api_keys:
            key = k.get("key", "")
            masked = key[:8] + "..." if len(key) > 8 else "***"
            print(f"{k.get('name', ''):<20} {masked:<18} {k.get('created_at', '')}")
        return
    if sub == "create":
        import secrets as _sec

        if any(k.get("name") == args.name for k in api_keys):
            print(f"✗ key '{args.name}' 已存在", file=sys.stderr)
            sys.exit(1)
        key = "sk-" + _sec.token_urlsafe(32)
        api_keys.append(
            {"name": args.name, "key": key,
             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        )
        cfg["api_keys"] = api_keys
        _cfg.save_config(cfg)
        print(f"✓ 已创建 '{args.name}': {key}")
        if args.copy:
            _to_clipboard(key)
            print("（已复制到剪贴板）")
        return
    if sub == "reveal":
        for k in api_keys:
            if k.get("name") == args.name:
                print(k.get("key", ""))
                return
        print(f"✗ key '{args.name}' 不存在", file=sys.stderr)
        sys.exit(1)
    if sub == "revoke":
        before = len(api_keys)
        cfg["api_keys"] = [k for k in api_keys if k.get("name") != args.name]
        if len(cfg["api_keys"]) == before:
            print(f"✗ key '{args.name}' 不存在", file=sys.stderr)
            sys.exit(1)
        _cfg.save_config(cfg)
        print(f"✓ 已吊销 '{args.name}'")
        return


def _to_clipboard(text: str) -> None:
    try:  # Windows
        import subprocess

        subprocess.run("clip", input=text.encode(), check=True, timeout=5)
    except Exception:  # noqa: BLE001 — 剪贴板是加分项，失败不致命
        try:  # Linux/macOS
            import subprocess as _sp

            _sp.run(["pbcopy"], input=text.encode(), check=True, timeout=5)
        except Exception:  # noqa: BLE001
            print("（剪贴板不可用，请手动复制）")


# ── config ─────────────────────────────────────────────────────────────


def cmd_config(args) -> None:
    base = _server_base()
    if base and args.config_cmd in ("get", "list"):
        data = _http(base, "GET", "/api/config")
        cfg = data.get("config", data)
    else:
        import config as _cfg

        cfg = _cfg.load_config()
        if base is None and args.config_cmd == "set":
            print("（服务未运行 → 离线直改 config.json，服务下次启动生效）")

    if args.config_cmd == "list":
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return
    if args.config_cmd == "get":
        val = cfg
        for seg in args.key.split("."):
            if not isinstance(val, dict) or seg not in val:
                print(f"✗ 无字段 {args.key}", file=sys.stderr)
                sys.exit(1)
            val = val[seg]
        print(val if not isinstance(val, (dict, list)) else json.dumps(val, ensure_ascii=False))
        return
    if args.config_cmd == "set":
        # set 始终直改文件（PUT /api/config 是整体替换，字段级 set 走文件更稳）
        import config as _cfg

        old = cfg.get(args.key)
        try:
            new = json.loads(args.value)
        except json.JSONDecodeError:
            new = args.value
        cfg[args.key] = new
        _cfg.save_config(cfg)
        print(f"✓ {args.key} = {json.dumps(new, ensure_ascii=False)}（原值 {json.dumps(old, ensure_ascii=False)}）")
        if base:
            print(f"⚠ 服务运行中（{base}），30s 配置缓存窗口内勿在 Web UI 改配置")
        return


# ── providers ──────────────────────────────────────────────────────────


def cmd_providers(args) -> None:
    base = _server_base()
    if base:
        data = _http(base, "GET", "/api/config")
        cfg = data.get("config", data)
    else:
        import config as _cfg

        cfg = _cfg.load_config()
    providers = cfg.get("providers", [])
    if not providers:
        print("（无 provider）")
        return
    print(f"{'NAME':<16} {'TYPE':<12} {'BASE_URLS':<40} ENCRYPTED")
    for p in providers:
        name = p.get("name", "")
        ptype = p.get("type", "")
        urls = (", ".join(f"{k}:{v}" for k, v in p.get("base_urls", {}).items()))[:38] \
            if isinstance(p.get("base_urls"), dict) else str(p.get("base_urls", ""))[:38]
        print(f"{name:<16} {ptype:<12} {urls:<40} {'✓' if p.get('encrypted') else '✗'}")


# ── main ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog="cyRouter", description="cyRouter 命令行控制")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_key = sub.add_parser("key", aliases=["keys"], help="密钥管理")
    key_sub = p_key.add_subparsers(dest="key_cmd", required=True)
    key_sub.add_parser("list", help="列出（掩码）")
    p_create = key_sub.add_parser("create", help="创建")
    p_create.add_argument("name")
    p_create.add_argument("--copy", action="store_true", help="创建后复制到剪贴板")
    p_rev = key_sub.add_parser("reveal", help="显示完整密钥")
    p_rev.add_argument("name")
    p_revoke = key_sub.add_parser("revoke", help="吊销")
    p_revoke.add_argument("name")
    p_key.set_defaults(func=cmd_key)

    p_tok = sub.add_parser("token", help="显示 admin token（dashboard/调试用）")
    p_tok.set_defaults(func=cmd_token)

    p_cfg = sub.add_parser("config", help="配置读写")
    cfg_sub = p_cfg.add_subparsers(dest="config_cmd", required=True)
    cfg_sub.add_parser("list", help="全部配置（JSON）")
    p_get = cfg_sub.add_parser("get", help="读字段（点路径）")
    p_get.add_argument("key")
    p_set = cfg_sub.add_parser("set", help="写字段（JSON 值自动解析）")
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_cfg.set_defaults(func=cmd_config)

    p_prv = sub.add_parser("providers", aliases=["provider"], help="provider 一览")
    p_prv.add_argument("dummy", nargs="?", help="（忽略，习惯性 list 占位）")
    p_prv.set_defaults(func=cmd_providers)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
