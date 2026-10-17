#!/usr/bin/env bash
# publish.sh: put vix on the internet through the existing Cloudflare tunnel (same steps as cfpublish).
#   ./publish.sh            HQ at https://vix.fueldeskpro.com (password sign-in) and MCP at https://vix-mcp.fueldeskpro.com (token)
#   ./publish.sh --status   show the published hostnames and whether they answer
#   ./publish.sh --remove   take both offline again
# Overrides: VIX_DOMAIN, VIX_HQ_HOST, VIX_MCP_HOST, CF_CONFIG, CF_LABEL. Safe to re-run; backs up the tunnel config first.
# DNS records are not created with 'cloudflared tunnel route dns' (that login can point at another zone); the script
# prints the two CNAME records to add in the Cloudflare dashboard and waits for them.
set -u
cd "$(dirname "$0")"
CFG="${CF_CONFIG:-/etc/cloudflared/config.yml}"
LABEL="${CF_LABEL:-com.cloudflare.cloudflared}"
DOMAIN="${VIX_DOMAIN:-fueldeskpro.com}"
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }
envget() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"'"'" || true; }
envset() { python3 - "$1" "$2" <<'PY'
import sys, pathlib
k, v = sys.argv[1:]; p = pathlib.Path(".env")
lines = p.read_text().splitlines() if p.exists() else []
out, done = [], False
for l in lines:
    if l.startswith(k + "="):
        if not done: out.append(f"{k}={v}"); done = True
    else: out.append(l)
if not done: out.append(f"{k}={v}")
p.write_text("\n".join(out) + "\n")
PY
}
cget() { curl --doh-url https://1.1.1.1/dns-query "$@" 2>/dev/null || curl "$@"; }

[ -d .venv ] || die "run this from ~/Sites/vix after install.sh"
[ -f "$CFG" ] || die "no tunnel config at $CFG (set CF_CONFIG=/path/to/config.yml)"
TUNNEL="$(grep -E '^tunnel:' "$CFG" | awk '{print $2}' | tr -d '"'"'")"
[ -n "$TUNNEL" ] || die "couldn't find 'tunnel:' in $CFG"
UUID="$TUNNEL"
if ! [[ "$UUID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  CRED="$(grep -E '^credentials-file:' "$CFG" | awk '{print $2}' | tr -d '"'"'")"
  UUID="$(sudo python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["TunnelID"])' "$CRED" 2>/dev/null || echo "$TUNNEL")"
fi
WEB="$(envget VIX_WEB_PORT)"; WEB="${WEB:-47530}"
MCP="$(envget VIX_MCP_PORT)"; MCP="${MCP:-47531}"
OLDMCPHOST="$(envget VIX_MCP_PUBLIC_HOST)"
HQHOST="${VIX_HQ_HOST:-vix.$DOMAIN}"
MCPHOST="${VIX_MCP_HOST:-${OLDMCPHOST:-vix-mcp.$DOMAIN}}"

if [ "${1:-}" = "--status" ]; then
  awk '/hostname:/{h=$NF} /service:/{ if(h!=""){print "  https://" h "  →  " $NF; h=""} }' "$CFG" | grep -E "$HQHOST|$MCPHOST" || echo "  not published"
  echo "  HQ  https://$HQHOST/  → HTTP $(cget -s -o /dev/null -m 8 -w '%{http_code}' "https://$HQHOST/")"
  echo "  MCP https://$MCPHOST/mcp → HTTP $(cget -s -o /dev/null -m 8 -w '%{http_code}' "https://$MCPHOST/mcp")"
  echo "  HQ password: $(.venv/bin/python -m vix.hqauth --status)"
  exit 0
fi

MODE=add; [ "${1:-}" = "--remove" ] && MODE=remove
echo "$MODE  $HQHOST → localhost:$WEB   $MCPHOST → localhost:$MCP   (tunnel $TUNNEL)"

# 1. Sign-in for HQ and a token for MCP (only on add)
if [ "$MODE" = add ]; then
  .venv/bin/python -m vix.hqauth --if-missing || die "HQ password not set; HQ would refuse internet visitors"
  ok "HQ password set (data/hq_auth.json, hashed)"
  TOKEN="$(envget VIX_MCP_TOKEN)"
  if [ -z "$TOKEN" ]; then TOKEN="$(openssl rand -hex 24)"; envset VIX_MCP_TOKEN "$TOKEN"; ok "new MCP token saved in .env"; else ok "keeping the MCP token already in .env"; fi
  envset VIX_MCP_PUBLIC_HOST "$MCPHOST"
else
  envset VIX_MCP_PUBLIC_HOST ""
fi

# 2. Restart vix so the MCP server picks up the host and token
./serve.sh >/dev/null 2>&1 || true
CODE="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$WEB/api/info")"
[ "$CODE" = 200 ] && ok "vix running locally" || warn "vix doesn't answer on 127.0.0.1:$WEB (see logs/web.log)"

# 3. Tunnel ingress rules (backup first)
sudo cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)" || die "sudo needed to edit $CFG"
sudo python3 - "$CFG" "$MODE" "$HQHOST" "http://localhost:$WEB" "$MCPHOST" "http://localhost:$MCP" <<'PY' || die "couldn't edit $CFG (backup kept next to it)"
import re, sys
path, mode, *pairs = sys.argv[1:]
lines = open(path).read().splitlines(keepends=True)
for host, svc in zip(pairs[0::2], pairs[1::2]):
    hre = re.compile(r'^(\s*)-\s*hostname:\s*["\']?' + re.escape(host) + r'["\']?\s*$')
    idx = next((i for i, l in enumerate(lines) if hre.match(l)), None)
    end = None
    if idx is not None:
        ind = hre.match(lines[idx]).group(1)
        end = idx + 1
        while end < len(lines) and not re.match(r'^' + re.escape(ind) + r'-\s', lines[end]) and (lines[end].startswith(ind + " ") or not lines[end].strip()):
            end += 1
    if mode == "remove":
        if idx is None: print(f"  {host}: not in config"); continue
        del lines[idx:end]; print(f"  {host}: removed"); continue
    if idx is not None:
        for j in range(idx + 1, end):
            if re.match(r'^\s*service:', lines[j]):
                lines[j] = re.sub(r'service:.*', 'service: ' + svc, lines[j].rstrip("\n")) + "\n"; break
        else:
            lines.insert(idx + 1, ind + "  service: " + svc + "\n")
        print(f"  {host}: updated existing rule")
    else:
        cidx = next((i for i, l in enumerate(lines) if re.match(r'^\s*-\s*service:\s*http_status:\d+', l)), None)
        if cidx is None: sys.exit("no catch-all '- service: http_status:404' rule found; add one at the end of ingress:")
        ind = re.match(r'^(\s*)', lines[cidx]).group(1)
        lines[cidx:cidx] = [f"{ind}- hostname: {host}\n", f"{ind}  service: {svc}\n"]
        print(f"  {host}: added rule")
open(path, "w").writelines(lines)
PY
sudo cloudflared tunnel --config "$CFG" ingress validate >/dev/null 2>&1 && ok "config valid" || {
  LAST="$(ls -t "$CFG".bak-* | head -1)"; sudo cp "$LAST" "$CFG"; die "config didn't validate; restored $LAST"; }
sudo launchctl kickstart -k "system/$LABEL" 2>/dev/null && ok "tunnel restarted" || warn "couldn't restart $LABEL; run: sudo launchctl kickstart -k system/$LABEL"

if [ "$MODE" = remove ]; then
  warn "also delete the CNAME records for ${HQHOST%.$DOMAIN} and ${MCPHOST%.$DOMAIN} in Cloudflare → $DOMAIN → DNS"; exit 0; fi

# 4. DNS: two proxied CNAMEs to the tunnel, added in the Cloudflare dashboard
missing=""
for h in "$HQHOST" "$MCPHOST"; do
  [ -n "$(dig +short @1.1.1.1 "$h" 2>/dev/null)" ] || missing="$missing $h"
done
if [ -n "$missing" ]; then
  warn "add these in Cloudflare → $DOMAIN → DNS → Add record (Proxy ON, orange cloud):"
  for h in $missing; do echo "       Type CNAME   Name ${h%.$DOMAIN}   Target $UUID.cfargotunnel.com"; done
  if [ -t 0 ]; then read -r -p "     Press Enter once they're saved… " _; fi
else
  ok "DNS records exist"
fi
sudo dscacheutil -flushcache 2>/dev/null; sudo killall -HUP mDNSResponder 2>/dev/null

# 5. Wait for both to answer through Cloudflare
printf '  waiting for https://%s ' "$HQHOST"
for i in $(seq 1 30); do
  CODE="$(cget -s -o /dev/null -m 8 -w '%{http_code}' "https://$HQHOST/")"
  case "$CODE" in 000|530|1033|502) printf '.'; sleep 4 ;; *) break ;; esac
done; echo
case "$CODE" in
  303|302|200) ok "HQ live: https://$HQHOST (asks for the HQ password)";;
  503) warn "HQ answers but says no password is set; run: .venv/bin/python -m vix.hqauth && ./serve.sh";;
  502) warn "HTTP 502: the tunnel reached the Mac but vix isn't answering on $WEB; run ./serve.sh";;
  404) warn "HTTP 404: the tunnel doesn't have the rule yet; run: sudo launchctl kickstart -k system/$LABEL";;
  000|530) warn "not reachable yet; DNS may still be propagating (Tailscale DNS caches misses). Try: ./publish.sh --status";;
  *) warn "HTTP $CODE from https://$HQHOST";;
esac
MCODE="$(cget -s -o /dev/null -m 8 -w '%{http_code}' "https://$MCPHOST/mcp")"
case "$MCODE" in
  401) ok "MCP live: https://$MCPHOST (refuses requests without the token, as it should)";;
  421|400) warn "MCP rejected the hostname (HTTP $MCODE); check VIX_MCP_PUBLIC_HOST in .env and run ./serve.sh";;
  *) warn "MCP: HTTP $MCODE from https://$MCPHOST/mcp; try ./publish.sh --status in a minute";;
esac
echo
echo "  claude.ai connector URL (keep it private, it contains the token):"
echo "     https://$MCPHOST/mcp/$(envget VIX_MCP_TOKEN)"
[ -n "$OLDMCPHOST" ] && [ "$OLDMCPHOST" = "$MCPHOST" ] && echo "     (same host and token as before, so an existing vix connector keeps working)"
echo "  Optional extra lock: Cloudflare Zero Trust → Access → Applications → Self-hosted → $HQHOST (email one-time code)."
echo "  Leave $MCPHOST out of Access: claude.ai can't pass an Access sign-in."
