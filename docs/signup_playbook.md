# LinkedIn Signup Playbook

Verified working pattern for creating a LinkedIn account on a proxy exit
(DataImpulse sticky residential). Last verified: 2026-10-19.

Winning exit: **71.247.73.181** (DataImpulse port `10032`) — LinkedIn
signup passed the Enterprise/captcha gate on this IP.

## TL;DR

- Pick a fresh, low-fraud US residential exit with **no** datacenter/VPN/proxy flags.
- The sticky session is **keyed by source IP** — vet and use it from the VPS only.
- Fresh Chrome profile, fresh email, new SMSPool US number.
- Skip Google pre-checks in the manual browser. Go straight to `linkedin.com/signup`.
- Checkbox green → Join within 30 s. No retries.
- Verify email + phone, warm up 5–15 min, stop the container, confirm `li_at`.
- Never visit `/login`, never `/logout`.

## Winning exit profile

| Metric | Value |
|---|---|
| IP / port | `71.247.73.181` / `10032` |
| ASN | AS701 Verizon Business (Fios) |
| Timezone | Eastern (matches `America/New_York`) |
| Scamalytics fraud | 16 (low risk) |
| Datacenter / VPN / Public Proxy / Anonymizing VPN / Tor | No / No / No / No / No |
| VPS browser canary | CLEAN, Cloudflare `loc=US` |

## 1. Vet exits on the VPS, not the laptop

Sticky sessions are keyed by `(port, source IP)`. The same port exits a
**different IP** from the laptop than from the VPS — a local test validates
nothing about the container's exit. Run every scan/canary on the VPS.

```bash
# Mapping check — run immediately before use (TTL can rotate mid-flow)
P="http://<PROXY_USER>:<PROXY_PASS>@gw.dataimpulse.com:10032"
curl -s --max-time 15 -x "$P" https://api.ipify.org; echo
curl -s --max-time 15 -x "$P" https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|loc)='
```

Accept only: expected IP + `loc=US`.

Sweep (`/tmp/dip_scan.sh 10008 10060`) and reject:

- fraud score >= 50
- Datacenter / VPN / Public Proxy / Anonymizing VPN = yes
- stale listings (100+ days); favor fresh IPs — the passing exit was ~2 days old
- wrong timezone or dirty geolocation (e.g. 2captcha UI in Vietnamese on a US IP)
- curl 200 alone is **not** enough (75.89.197.55 and 161.8.172.65 passed curl, failed in-browser)

## 2. VPS browser canary

`/tmp/dip_canary.py` mirrors the real launch config: `channel="chrome"`,
`headless=False`, `no_viewport=True`, `locale="en-US"`,
`timezone_id="America/New_York"`, proxy credentials as separate fields,
media blocked, fresh temp profile per port. It prints `port ip loc google`.

Only `CLEAN` + `loc=US` proceeds. The script is in `/tmp` (ephemeral) —
recreate it if missing.

**This canary's Google search is the last Google touch you make from that IP.**
Do not re-probe Google in the manual browser: repeated searches from one
sticky exit trigger Google search captchas that do not predict LinkedIn's
Enterprise score.

## 3. Hard pre-signup gates

- Fresh email, never used on LinkedIn (not an alias of a burned one).
- New SMSPool US number (LinkedIn pool, non-VoIP, ~$0.14).
- Container logs show the right exit:
  ```bash
  docker logs oo-signup 2>&1 | tail -5
  # expect: tz=America/New_York locale=en-US, Exit IP: 71.247.73.181
  ```
- Wipe the profile (bind mount survives `docker run`, so wiping is mandatory):
  ```bash
  docker stop oo-signup 2>/dev/null; docker rm oo-signup 2>/dev/null
  rm -rf /opt/openoutreach/acct1/data/browser-profile
  rm -f /opt/openoutreach/acct1/data/storage_state.json
  chown -R 1000:1000 /opt/openoutreach/acct1/data
  ```

## 4. Launch the manual browser

Container `oo-signup`, image `openoutreach:acct1`,
`-p 127.0.0.1:6082:6080`, data volume `/opt/openoutreach/acct1/data:/app/data`,
cache volume, `--name oo-signup`.

Env checklist:

```
TZ=America/New_York
BROWSER_TIMEZONE_ID=America/New_York
BROWSER_LOCALE=en-US
BROWSER_BLOCKED_RESOURCE_TYPES=media
PROXY_URL=http://<PROXY_USER>:<PROXY_PASS>@gw.dataimpulse.com:10032
PYTHONUNBUFFERED=1
```

Command: `Xvfb :99` + `x11vnc` + `websockify` on 6080, then
`exec python manage.py manual_browser --save-state /app/data/storage_state.json`
(`--url` defaults to `https://www.linkedin.com/signup`).

VNC: `ssh -L 6082:localhost:6082 root@vmi2437711` →
`http://localhost:6082/vnc.html`.

## 5. Signup flow

- `manual_browser` opens `linkedin.com/signup` automatically. One tab.
- Fill the form with the fresh persona; keep the SMSPool number in hand.
- Solve the checkbox. Green → click Join **within 30 s**.
- No edits or resubmits after solving. Any error
  ("response code missing or invalid") → close the tab, screenshot, stop.
  No in-place retry.
- Email verification → phone verification.
- Warm up 5–15 min (gentle browsing, no invitations).
- `docker stop oo-signup` (graceful; writes `storage_state` on SIGTERM).
- Confirm `li_at` is present in `/app/data/storage_state.json`.

## Red herrings (learned the hard way)

- **Google search captcha in the manual browser is not a LinkedIn veto.**
  `71.247.73.181` passed LinkedIn after repeated search captchas. The
  manual browser loads LinkedIn's Enterprise assets before you check Google,
  and repeated probes worsen search scoring.
- **Enterprise demo green is not a LinkedIn pass.** 212.56.54.202 passed the
  full 2captcha Enterprise demo and LinkedIn still rejected its token.
- **Curl 200 is not browser-clean** (75.89.197.55, 161.8.172.65).
- **2captcha UI language is client-side geolocation**, not server
  Accept-Language. Vietnamese on a US IP = dirty geo intel (212.56.54.202).
- **Mid-flow rotation kills the account.** Attempt #1 passed signup, email,
  and phone on 92.112.140.177, then hit Persona restriction because the
  sticky exit rotated to 38.143.188.48. DataImpulse TTL is <= 120 min
  (avg ~30), so it is practice-only.
- **Freshness matters**: the passing exit was recent; 242/278-day listings
  failed.

## Durable accounts

One static ISP residential IP per account, used for **both** signup and the
daemon, with `EXPECTED_EXIT_IP` set to fail fast on drift. Never attach a
signed-in account to a rotating sticky session.

## VPS paths

- Repo: `/opt/openoutreach/repo`
- Instance data: `/opt/openoutreach/acct1/data`
- Sweep script: `/tmp/dip_scan.sh`
- Browser canary: `/tmp/dip_canary.py`
