# cloudflare-mcp

A single-file Model Context Protocol (MCP) server by Syed Abrar Ali that lets an MCP client (a chat assistant or any MCP-capable tool) operate one Cloudflare account: run Wrangler commands for Workers, KV, R2, D1, Pages and logs; deploy a one-file Worker straight from source; manage the DNS records of one zone; and publish or unpublish local services as public hostnames on a Cloudflare Tunnel. It exists because these tasks are repetitive and error-prone from a terminal, but letting an assistant do them blindly could take a live site offline. The server therefore reads freely but requires `confirm=true` (after a human has agreed) for anything that deletes, overwrites, or touches a hostname that keeps a live service running.

## Where it is used

It runs on the owner's laptop next to several other personal MCP servers. Over stdio it is used by local MCP clients; in HTTP mode it listens on the loopback interface and an Apache reverse-proxy gateway exposes it under a secret path so a remote chat assistant can connect. Its day-to-day role is to add or move subdomains, point a new tunnel hostname at a local port, deploy small Workers, and inspect deployments and logs without opening the Cloudflare dashboard.

## How it works

All logic is in `server.py`, built on `mcp.server.fastmcp.FastMCP`.

| Piece | Role |
| --- | --- |
| `CF`, `ACCOUNT`, `ZONE`, `TUNNEL` | Loaded at import from a JSON file under `~/.config/cloudflare/` (path hard-coded near the top of the file) with keys `account_id`, `zone_id`, `tunnel_id`. |
| `DOMAIN` | The zone's apex domain (constant). `_fqdn("api")` turns short names into `api.<DOMAIN>`. |
| `PROTECTED` | Hostnames of live sites; changing their records or routes needs `confirm=true`. |
| `_token()` | Reads an API token from `~/.config/cloudflare/token` for REST calls. Never returned. |
| `_api(method, path, body)` | `urllib` wrapper around `https://api.cloudflare.com/client/v4`; unwraps the `success`/`errors`/`result` envelope and raises a readable `RuntimeError` on failure. |
| `_run_wrangler(args, cwd)` | Runs `npx -y wrangler@4 ...` with `CI=1`, metrics off, `NO_COLOR`, and `CLOUDFLARE_ACCOUNT_ID` set. It deliberately removes `CLOUDFLARE_API_TOKEN` from the environment so Wrangler uses the owner's own OAuth login. |
| `_clip()` | Truncates output to 12,000 characters and masks anything that looks like `token/secret/key = <long string>`. |
| `_DESTRUCTIVE` | Regex (`delete`, `rollback`, `remove`, `rm`, `purge`, `destroy`, `drop`, `truncate`) used to classify risky Wrangler commands. |

Tools:

| Tool | Behaviour | Guard |
| --- | --- | --- |
| `whoami` | `wrangler whoami` | — |
| `wrangler(command)` | Any Wrangler subcommand. `login`, `logout`, `init`, `dev` are refused. `tail` is cut off after 60 s, everything else after 300 s. | `confirm=true` if the command matches `_DESTRUCTIVE`, is `secret put`, or is `deploy` |
| `deploy_worker(name, code, compatibility_date, route)` | Writes `index.js` and a generated `wrangler.toml` into a temp dir and runs `wrangler deploy`. Optional `route` pattern attaches it to the zone. | `confirm=true` if a Worker of that name already exists or the route host is protected |
| `list_dns_records(name_contains)` | Pages through all records of the zone (100 per page), optional substring filter | — |
| `create_dns_record(type, name, content, proxied, ttl, priority, comment)` | Creates A/AAAA/CNAME/TXT/MX/...; `proxied` only applied to A/AAAA/CNAME | — |
| `update_dns_record(record_id, content, proxied, ttl)` | PATCHes an existing record | `confirm=true` if the record belongs to a protected hostname |
| `delete_dns_record(record_id)` | Deletes a record | `confirm=true` |
| `list_tunnel_hostnames` | Reads the tunnel's ingress configuration and lists hostname → service | — |
| `add_tunnel_hostname(hostname, service)` | Inserts an ingress rule before the catch-all, PUTs the config back, and creates a proxied CNAME to `<tunnel-id>.cfargotunnel.com` if no record exists | `confirm=true` if the hostname is already published |
| `remove_tunnel_hostname(hostname)` | Removes the ingress rule (the DNS record is left in place on purpose) | `confirm=true` |

Decision rule: guarded calls made without `confirm=true` return a `needs_confirmation` message describing the exact change instead of failing, so the model can relay it and retry once approved. The server keeps no state of its own.

## Setup

Requirements:

- Python 3.10+, `pip install "mcp[cli]"`
- Node.js with `npx` (Wrangler 4 is fetched on demand by `npx -y wrangler@4`)
- a Wrangler OAuth login: `npx wrangler@4 login`
- a Cloudflare API token with DNS edit and Tunnel edit rights for the REST tools
- a Cloudflare Tunnel already created (remotely managed) whose id you know

Files to create:

```bash
mkdir -p ~/.config/cloudflare
printf '%s' 'YOUR_API_TOKEN' > ~/.config/cloudflare/token && chmod 600 ~/.config/cloudflare/token
cat > ~/.config/cloudflare/ZONE.json <<'EOF'
{ "account_id": "ACCOUNT_ID", "zone_id": "ZONE_ID", "tunnel_id": "TUNNEL_ID" }
EOF
```

Then edit the constants at the top of `server.py`: the JSON file name, `DOMAIN = "example.com"`, the `PROTECTED` hostnames, and (in `main()`) the `allowed_hosts` / `allowed_origins` for your gateway hostname. `CLOUDFLARE_MCP_PORT` (default 8978) can also be set in the environment.

## Usage

```bash
python server.py                       # stdio, for local MCP clients
python server.py --http --port 8978    # streamable HTTP at http://127.0.0.1:8978/mcp
```

Example interactions:

- "What's deployed?" → `wrangler("deployments list --name my-worker")`
- "Publish the service on port 8080 as demo" → `add_tunnel_hostname("demo", "http://127.0.0.1:8080")` creates both the ingress rule and `demo.example.com` CNAME
- "Point api at the new IP" → `list_dns_records("api")` to get the id, then `update_dns_record(id, content="REDACTED_IP")`
- "Deploy this Worker" → `deploy_worker("hello", "export default { fetch() { return new Response('hi') } }")`
- "Delete the old TXT record" → `delete_dns_record(id)` returns `needs_confirmation`; after the user agrees, call again with `confirm=true`

## Project structure

```
cloudflare-mcp/
└── server.py     # FastMCP server: Wrangler runner, Cloudflare REST helpers, DNS + tunnel tools
```

## Limitations and notes

- One account, one zone, one tunnel per server instance; all three ids are read once at import.
- Wrangler commands run through `npx`, so the first call after a cache clear downloads Wrangler and is slow; `tail` always stops after 60 seconds.
- `deploy_worker` handles a single ES-module file with no bindings; anything with KV/D1/R2 bindings needs a real project and the `wrangler` tool.
- `remove_tunnel_hostname` intentionally leaves the DNS record behind; delete it separately if wanted.
- The destructive-command check is a word match on the command string, not a full parse; treat it as a safety net, not a guarantee.
- The HTTP transport has no authentication of its own and binds to loopback; it expects a fronting proxy with a secret path and TLS.
- Server instructions and confirmation prompts address the owner by first name; adjust for other deployments.
