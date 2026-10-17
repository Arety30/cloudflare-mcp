"""
Cloudflare MCP — lets ChatGPT (or any MCP client) work on Abrar's Cloudflare account through Wrangler and the API.

Tools: whoami, wrangler (any Wrangler command), deploy_worker (a one-file Worker from code), DNS records for
example.com (list / create / update / delete), and the tunnel's public hostnames (list / add / remove).
Anything that deletes, overwrites or takes something offline needs confirm=true, and the instructions tell the
model to get Abrar's OK first.

Transports:
    python server.py                         -> stdio
    python server.py --http [--port 8978]    -> streamable HTTP at /mcp on 127.0.0.1 (published as
                                                https://example.com/cloudflare/<token> by the Apache gateway)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

CF = json.loads((Path.home() / ".config/cloudflare/arety-cloud.json").read_text())
ACCOUNT, ZONE, TUNNEL = CF["account_id"], CF["zone_id"], CF["tunnel_id"]
DOMAIN = "example.com"
NPX = shutil.which("npx") or "npx"
MAX_TEXT = 12000
# hostnames that keep Abrar's live services up: changing them always needs confirm=true
PROTECTED = {"example.com", "example.com", "example.com", "example.com",
             "example.com", "example.com", "example.com"}

mcp = FastMCP(
    "cloudflare",
    instructions=(
        "Tools for Abrar's Cloudflare account (zone example.com). Use `wrangler` for Workers, KV, R2, D1, Pages, queues "
        "and logs; the DNS and tunnel tools for records and public hostnames. Read first (list/whoami) before changing "
        "anything. Deleting, overwriting a Worker, removing a hostname or changing a record of a live site is "
        "permanent or takes something offline: describe exactly what will change, get Abrar's explicit OK, then call "
        "the tool again with confirm=true. Never print API tokens or secrets in replies."
    ),
)


def _token() -> str:
    return (Path.home() / ".config/cloudflare/token").read_text().strip()


def _api(method: str, path: str, body: Any = None) -> Any:
    req = urllib.request.Request("https://api.cloudflare.com/client/v4" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        data = json.loads(e.read() or b"{}")
    if not data.get("success", False):
        raise RuntimeError("; ".join(f"{x.get('code')}: {x.get('message')}" for x in data.get("errors", [])) or "Cloudflare API error")
    return data.get("result")


def _clip(s: str) -> str:
    s = re.sub(r"(?i)(token|secret|key)(\W{1,3})[A-Za-z0-9_\-\.]{20,}", r"\1\2***", s or "")
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + f"\n… ({len(s) - MAX_TEXT} more characters)"


def _fqdn(name: str) -> str:
    name = name.strip().rstrip(".").lower()
    return name if name == DOMAIN or name.endswith("." + DOMAIN) else f"{name}.{DOMAIN}"


_DESTRUCTIVE = re.compile(r"\b(delete|rollback|remove|rm|purge|destroy|drop|truncate)\b", re.I)


def _run_wrangler(args: list[str], cwd: str | None = None, timeout: int = 300) -> str:
    env = dict(os.environ, CI="1", WRANGLER_SEND_METRICS="false", NO_COLOR="1", CLOUDFLARE_ACCOUNT_ID=ACCOUNT)
    env.pop("CLOUDFLARE_API_TOKEN", None)                       # Wrangler uses Abrar's own OAuth login
    p = subprocess.run([NPX, "-y", "wrangler@4", *args], cwd=cwd or tempfile.gettempdir(), env=env,
                       capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr.strip() else "")
    return _clip(f"exit {p.returncode}\n{out.strip()}")


# ------------------------------------------------------------------ account ----
@mcp.tool()
def whoami() -> str:
    """Which Cloudflare account Wrangler is logged into, and its permissions."""
    return _run_wrangler(["whoami"], timeout=120)


@mcp.tool()
def wrangler(command: str, confirm: bool = False) -> str:
    """Run any Wrangler command, e.g. "deployments list --name my-worker", "kv namespace list", "r2 bucket list",
    "d1 list", "tail my-worker --format json" (tail stops after 60 s). Commands that delete, roll back, purge
    or overwrite secrets need confirm=true (ask Abrar first). login/logout are not available."""
    args = shlex.split(command.strip().removeprefix("wrangler").strip())
    if not args:
        return "Give a Wrangler command, for example: kv namespace list"
    if args[0] in ("login", "logout", "init", "dev"):
        return f"'{args[0]}' is not available here."
    risky = bool(_DESTRUCTIVE.search(command)) or (args[:2] == ["secret", "put"]) or args[0] == "deploy"
    if risky and not confirm:
        return (f"This command can delete or overwrite things: `wrangler {command}`. Describe it to Abrar, get his OK, "
                "then call again with confirm=true.")
    timeout = 60 if args[0] == "tail" else 300
    try:
        return _run_wrangler(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"Stopped after {timeout} s." + (" (tail streams forever; this is expected)" if args[0] == "tail" else "")


@mcp.tool()
def deploy_worker(name: str, code: str, compatibility_date: str = "2025-09-01", route: str = "", confirm: bool = False) -> str:
    """Deploy a one-file Worker (ES module JavaScript with `export default { fetch(...) }`). It is published at
    https://<name>.<account>.workers.dev, plus `route` (e.g. "example.com/*") if given. Replacing an existing
    Worker needs confirm=true."""
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        return "Worker names use lower-case letters, digits and dashes."
    existing = _run_wrangler(["deployments", "list", "--name", name], timeout=120)
    if "exit 0" in existing.splitlines()[0] and not confirm:
        return f"A Worker called '{name}' already exists. Ask Abrar before replacing it, then call again with confirm=true."
    if route and _fqdn(route.split("/")[0]) in PROTECTED and not confirm:
        return f"{route.split('/')[0]} is a live site. Ask Abrar first, then call again with confirm=true."
    with tempfile.TemporaryDirectory() as d:
        Path(d, "index.js").write_text(code)
        toml = f'name = "{name}"\nmain = "index.js"\ncompatibility_date = "{compatibility_date}"\naccount_id = "{ACCOUNT}"\n'
        if route:
            toml += f'routes = [{{ pattern = "{route}", zone_name = "{DOMAIN}" }}]\n'
        Path(d, "wrangler.toml").write_text(toml)
        return _run_wrangler(["deploy"], cwd=d, timeout=300)


# ------------------------------------------------------------------ DNS ----
@mcp.tool()
def list_dns_records(name_contains: str = "") -> list[dict]:
    """DNS records of example.com (optionally only names containing some text)."""
    out, page = [], 1
    while True:
        res = _api("GET", f"/zones/{ZONE}/dns_records?per_page=100&page={page}")
        out += [{"id": r["id"], "type": r["type"], "name": r["name"], "content": r["content"], "proxied": r.get("proxied"),
                 "ttl": r.get("ttl"), "comment": r.get("comment")} for r in res if name_contains.lower() in r["name"].lower()]
        if len(res) < 100:
            return out
        page += 1


@mcp.tool()
def create_dns_record(type: str, name: str, content: str, proxied: bool = True, ttl: int = 1, priority: int | None = None,
                      comment: str = "") -> dict:
    """Create a DNS record in example.com (type A, AAAA, CNAME, TXT, MX, ...). `name` may be "api" or "example.com"."""
    body: dict[str, Any] = {"type": type.upper(), "name": _fqdn(name), "content": content, "ttl": ttl, "comment": comment or "via MCP"}
    if body["type"] in ("A", "AAAA", "CNAME"):
        body["proxied"] = proxied
    if priority is not None:
        body["priority"] = priority
    r = _api("POST", f"/zones/{ZONE}/dns_records", body)
    return {"created": True, "id": r["id"], "name": r["name"], "type": r["type"], "content": r["content"]}


@mcp.tool()
def update_dns_record(record_id: str, content: str = "", proxied: bool | None = None, ttl: int | None = None,
                      confirm: bool = False) -> dict:
    """Change an existing record (find its id with list_dns_records). Records of live sites need confirm=true."""
    cur = _api("GET", f"/zones/{ZONE}/dns_records/{record_id}")
    if cur["name"] in PROTECTED and not confirm:
        return {"needs_confirmation": f"{cur['name']} is a live site. Ask Abrar, then call again with confirm=true."}
    body: dict[str, Any] = {}
    if content:
        body["content"] = content
    if proxied is not None:
        body["proxied"] = proxied
    if ttl is not None:
        body["ttl"] = ttl
    r = _api("PATCH", f"/zones/{ZONE}/dns_records/{record_id}", body)
    return {"updated": True, "name": r["name"], "type": r["type"], "content": r["content"], "proxied": r.get("proxied")}


@mcp.tool()
def delete_dns_record(record_id: str, confirm: bool = False) -> dict:
    """Delete a DNS record (permanent). Needs confirm=true after Abrar agrees."""
    cur = _api("GET", f"/zones/{ZONE}/dns_records/{record_id}")
    if not confirm:
        return {"needs_confirmation": f"Deleting {cur['type']} {cur['name']} → {cur['content']} is permanent. Ask Abrar, then call again with confirm=true."}
    _api("DELETE", f"/zones/{ZONE}/dns_records/{record_id}")
    return {"deleted": True, "name": cur["name"], "type": cur["type"]}


# ------------------------------------------------------------------ tunnel ----
def _tunnel_config() -> dict:
    return _api("GET", f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/configurations")["config"]


@mcp.tool()
def list_tunnel_hostnames() -> list[dict]:
    """Public hostnames served through Abrar's Cloudflare tunnel, and where each one points."""
    return [{"hostname": r.get("hostname"), "path": r.get("path"), "service": r.get("service")}
            for r in _tunnel_config()["ingress"] if r.get("hostname")]


@mcp.tool()
def add_tunnel_hostname(hostname: str, service: str, confirm: bool = False) -> dict:
    """Publish a local service through the tunnel, e.g. hostname "demo" -> service "http://127.0.0.1:8080".
    Also creates the proxied DNS record. Replacing an existing hostname needs confirm=true."""
    host = _fqdn(hostname)
    cfg = _tunnel_config()
    ing = cfg["ingress"]
    existing = [r for r in ing if r.get("hostname") == host]
    if existing and not confirm:
        return {"needs_confirmation": f"{host} already points to {existing[0].get('service')}. Ask Abrar, then call again with confirm=true."}
    ing[:] = [r for r in ing if r.get("hostname") != host]
    ing.insert(len(ing) - 1, {"hostname": host, "service": service})
    _api("PUT", f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/configurations", {"config": cfg})
    if not _api("GET", f"/zones/{ZONE}/dns_records?name={host}"):
        _api("POST", f"/zones/{ZONE}/dns_records", {"type": "CNAME", "name": host, "content": f"{TUNNEL}.cfargotunnel.com",
                                                     "proxied": True, "comment": "tunnel hostname via MCP"})
    return {"published": host, "service": service}


@mcp.tool()
def remove_tunnel_hostname(hostname: str, confirm: bool = False) -> dict:
    """Stop serving a hostname through the tunnel (takes that site offline). Needs confirm=true."""
    host = _fqdn(hostname)
    if not confirm:
        return {"needs_confirmation": f"Removing {host} takes it offline. Ask Abrar, then call again with confirm=true."}
    cfg = _tunnel_config()
    before = len(cfg["ingress"])
    cfg["ingress"] = [r for r in cfg["ingress"] if r.get("hostname") != host]
    if len(cfg["ingress"]) == before:
        return {"removed": False, "reason": f"{host} is not on the tunnel"}
    _api("PUT", f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/configurations", {"config": cfg})
    return {"removed": True, "hostname": host, "note": "its DNS record was left in place; delete it with delete_dns_record if wanted"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CLOUDFLARE_MCP_PORT", "8978")))
    a = ap.parse_args()
    if not a.http:
        mcp.run()
        return
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = a.port
    mcp.settings.streamable_http_path = "/mcp"            # the secret token lives in the gateway path
    try:
        mcp.settings.transport_security.allowed_hosts = ["example.com", "127.0.0.1:*", "localhost:*"]
        mcp.settings.transport_security.allowed_origins = ["https://example.com", "https://chatgpt.com", "https://chat.openai.com"]
    except Exception:  # noqa: BLE001
        pass
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
