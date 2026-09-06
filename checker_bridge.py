"""
shopify_bridge.py — Async HTTP load balancer for Shopify checker nodes.

Each node runs the Shopify checker Flask API on :5000 with /shopify endpoint.
Routing uses least-connections + circuit-breaker with retries across healthy nodes.
"""

import os
import asyncio
import aiohttp
import time
import logging
from urllib.parse import quote as _urlquote, urlparse

log = logging.getLogger("shopify_bridge")
log.setLevel(logging.DEBUG)

# ── Node list ────────────────────────────────────────────────────────────────
# NOTE: never leave an empty string here — it gets picked as a node and every
# request routed to it fails instantly, wasting half the attempts.
NODES = [u for u in [
    "http://hornyneon.up.railway.app",
] if u.strip()]

# ── Disabled nodes ────────────────────────────────────────────────────────────
_disabled_nodes: set = set()

# ── Per-node state ────────────────────────────────────────────────────────────
_state: dict = {
    url: {
        "in_flight": 0,
        "consec_fails": 0,
        "healthy": True,
        "unhealthy_at": 0.0,
        "avg_ms": 3000.0,
        "total_ok": 0,
    }
    for url in NODES
}

_CIRCUIT_FAIL_THRESHOLD = 5
_CIRCUIT_RESET_SECS = 20.0
_REQUEST_TIMEOUT = 60
_CONNECT_TIMEOUT = 8
_HEALTH_PING_INTERVAL = 15

# ── Global concurrency cap ───────────────────────────────────────────────────
# Was 8 — that was the real speed cap. api.py runs under waitress with a large
# thread pool and each check is ~10–15s wall time but almost all network wait,
# so the node chews a lot in parallel. 400 keeps the Railway node saturated
# with a 40–70 proxy pool (≈3 in-flight checks per proxy) without OOMing.
# Override at runtime with BRIDGE_MAX_CONCURRENT.
MAX_CONCURRENT_REQUESTS = int(os.environ.get("BRIDGE_MAX_CONCURRENT", "400"))

_request_sem: asyncio.Semaphore | None = None


def _get_request_sem() -> asyncio.Semaphore:
    global _request_sem
    if _request_sem is None:
        _request_sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return _request_sem

_PROXY_BURNED_INDICATORS = (
    "proxy burned", "change your proxy", "proxy error",
    "authentication failed", "could not connect", "proxy dead",
)

_session: aiohttp.ClientSession | None = None


# ── Session management ──────────────────────────────────────────────────────

async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        conn = aiohttp.TCPConnector(
            limit=MAX_CONCURRENT_REQUESTS * 2,
            limit_per_host=MAX_CONCURRENT_REQUESTS,
            ttl_dns_cache=300,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
        )
        _session = aiohttp.ClientSession(
            connector=conn,
            timeout=aiohttp.ClientTimeout(
                total=_REQUEST_TIMEOUT,
                connect=_CONNECT_TIMEOUT,
            ),
        )
    return _session


# ── Circuit-breaker helpers ─────────────────────────────────────────────────

def _maybe_reset(url: str) -> None:
    s = _state[url]
    if not s["healthy"] and (time.monotonic() - s["unhealthy_at"]) >= _CIRCUIT_RESET_SECS:
        s["healthy"] = True
        s["consec_fails"] = 0
        log.info(f"[lb] circuit RESET → {url}")


def _pick_node(exclude: set | None = None) -> str | None:
    exclude = exclude or set()
    for url in _state:
        _maybe_reset(url)

    cands = [(u, s) for u, s in _state.items()
             if u not in exclude and u not in _disabled_nodes]
    if not cands:
        return None
    healthy = [(u, s) for u, s in cands if s["healthy"]]
    pool = healthy if healthy else cands
    pool.sort(key=lambda x: (x[1]["in_flight"], x[1]["avg_ms"]))
    return pool[0][0]


# ── Single node HTTP call ──────────────────────────────────────────────────

async def _call_node(node: str, cc: str, proxy: str, site: str, variant: str | None = None) -> dict:
    s = _state[node]
    t0 = time.monotonic()
    cc4 = cc.split("|")[0][-4:] if "|" in cc else cc[-4:]
    s["in_flight"] += 1
    log.debug(f"[lb] → {node} | cc=...{cc4} | in_flight={s['in_flight']}")

    try:
        sess = await _get_session()

        params = {
            "cc": cc,
            "proxy": proxy,
            "site": site,
        }
        if variant:
            params["variant"] = variant

        sem = _get_request_sem()
        async with sem:
            async with sess.get(
                f"{node}/shopify",  # ✅ FIXED: was /check
                params=params,
                timeout=aiohttp.ClientTimeout(
                    total=_REQUEST_TIMEOUT, connect=_CONNECT_TIMEOUT,
                ),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.json(content_type=None)

        elapsed = (time.monotonic() - t0) * 1000
        s["consec_fails"] = 0
        s["healthy"] = True
        s["avg_ms"] = 0.75 * s["avg_ms"] + 0.25 * elapsed
        s["total_ok"] += 1
        log.debug(f"[lb] ✓ {node} | {elapsed:.0f}ms | resp={str(data.get('Response',''))[:60]}")
        return data

    except (asyncio.TimeoutError, TimeoutError) as e:
        elapsed = (time.monotonic() - t0) * 1000
        s["consec_fails"] += 1
        log.warning(f"[lb] TIMEOUT {node} | {elapsed:.0f}ms | fails={s['consec_fails']}")
        if s["consec_fails"] >= _CIRCUIT_FAIL_THRESHOLD:
            s["healthy"] = False
            s["unhealthy_at"] = time.monotonic()
            log.warning(f"[lb] OPEN (timeout) → {node}")
        raise

    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        err_str = str(e)[:80]
        err_type = type(e).__name__
        proxy_side = any(ind in err_str.lower() for ind in _PROXY_BURNED_INDICATORS)
        if not proxy_side:
            s["consec_fails"] += 1
            if s["consec_fails"] >= _CIRCUIT_FAIL_THRESHOLD:
                s["healthy"] = False
                s["unhealthy_at"] = time.monotonic()
                log.warning(f"[lb] OPEN ({err_type}) → {node}")
        log.warning(f"[lb] ERROR {node} | {elapsed:.0f}ms | {err_type}: {err_str}")
        raise

    finally:
        s["in_flight"] = max(0, s["in_flight"] - 1)


# ── Background health pinger ───────────────────────────────────────────────

async def _health_loop() -> None:
    while True:
        await asyncio.sleep(_HEALTH_PING_INTERVAL)
        for url in list(_state):
            try:
                sess = await _get_session()
                # ✅ FIXED: was /health, now just / to check if Flask is up
                async with sess.get(
                    f"{url}/",
                    timeout=aiohttp.ClientTimeout(total=6, connect=4),
                ) as r:
                    # Flask returns 404 on root, but that means it's alive
                    if r.status in (200, 404):
                        if not _state[url]["healthy"]:
                            log.info(f"[lb] RESTORED → {url}")
                        _state[url]["healthy"] = True
                        _state[url]["consec_fails"] = 0
            except Exception:
                pass


_health_task: asyncio.Task | None = None


def _ensure_health_loop() -> None:
    global _health_task
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running() and (_health_task is None or _health_task.done()):
            _health_task = loop.create_task(_health_loop())
    except Exception:
        pass


# ── Proxy helpers ──────────────────────────────────────────────────────────
#
# api.py's parse_proxy() ONLY accepts:
#     ip:port                    -> http://ip:port
#     ip:port:user:pass          -> http://user:pass@ip:port
# Anything else (URL form, user:pass@host:port, socks5://…) is silently
# dropped, so the node runs the check WITHOUT a proxy and burns the request.
# We therefore always emit one of those two shapes here.

def _split_url_form(s: str) -> tuple[str, str, str | None, str | None] | None:
    """Parse http(s)://[user:pass@]host:port -> (host, port, user, pass)."""
    try:
        u = urlparse(s)
        if not u.hostname or not u.port:
            return None
        return u.hostname, str(u.port), u.username, u.password
    except Exception:
        return None


def _proxy_data_to_proxy_str(proxy_data: dict | str | None) -> str | None:
    """Normalize any proxy input to the shape api.py understands.
       Returns 'ip:port' or 'ip:port:user:pass' (no scheme, colon-separated).
    """
    if not proxy_data:
        return None

    host = port = user = pw = None

    # ── string forms ────────────────────────────────────────────
    if isinstance(proxy_data, str):
        s = proxy_data.strip()
        if not s:
            return None

        # Strip scheme (http/https/socks*). SOCKS is downgraded to http-style
        # tuple because the upstream checker only speaks HTTP proxies.
        low = s.lower()
        if "://" in s:
            parsed = _split_url_form(s)
            if not parsed:
                return None
            host, port, user, pw = parsed
        elif "@" in s:
            # [user:pass]@host:port
            auth, _, hp = s.rpartition("@")
            hp_parts = hp.split(":")
            if len(hp_parts) != 2:
                return None
            host, port = hp_parts
            if ":" in auth:
                user, pw = auth.split(":", 1)
            else:
                user, pw = auth, ""
        else:
            parts = s.split(":")
            if len(parts) == 2:
                host, port = parts
            elif len(parts) == 4:
                host, port, user, pw = parts
            elif len(parts) == 3:
                # proto:host:port  (e.g. socks5:1.2.3.4:1080)
                _, host, port = parts
            elif len(parts) == 5:
                # proto:host:port:user:pass
                _, host, port, user, pw = parts
            else:
                return None

    # ── dict form ───────────────────────────────────────────────
    elif isinstance(proxy_data, dict):
        existing = proxy_data.get("proxy_url")
        if existing and isinstance(existing, str) and existing.strip():
            return _proxy_data_to_proxy_str(existing.strip())

        host = str(proxy_data.get("ip") or proxy_data.get("host") or "").strip()
        port = str(proxy_data.get("port") or "").strip()
        user = proxy_data.get("username") or proxy_data.get("user")
        pw = proxy_data.get("password") or proxy_data.get("pass")
    else:
        return None

    if not host or not port:
        return None
    try:
        int(port)
    except (TypeError, ValueError):
        return None

    if user and pw is not None:
        return f"{host}:{port}:{user}:{pw}"
    return f"{host}:{port}"


# ── Result normalisation ────────────────────────────────────────────────────

def _map_result(raw: dict, cc_str: str, site_url: str) -> dict:
    response = raw.get("Response") or raw.get("error") or "Unknown"
    if not isinstance(response, str):
        response = str(response)
    price = raw.get("Price", "-")
    gateway = raw.get("Gateway", "Shopify")
    status = raw.get("Status", False)

    resp_lower = response.lower()

    _SITE_SIDE = (
        "generic_error", "processing_error", "merchandise_expected_price_mismatch",
        "session token", "throttled", "checkpoint", "captcha_required",
        "site requires login", "site not supported", "payment method not available",
        "not shopify", "no products", "no valid products", "cart failed",
        "site error", "delivery", "shipping", "mismatched_bill",
        "http 404", "http 403", "http 429", "http 5",
        "delivery_delivery_line_detail_changed",
    )

    if any(m in resp_lower for m in _SITE_SIDE):
        result_status = "Site Error"
        live = False
    # ── Check response text BEFORE trusting Status boolean ──
    # The API sometimes returns Status:True even on CARD_DECLINED
    elif "declined" in resp_lower or "card_declined" in resp_lower:
        result_status = "Declined"
        live = False
    elif "insufficient" in resp_lower:
        result_status = "Declined"
        live = False
    elif "cvv" in resp_lower or "cvc" in resp_lower or "incorrect_cvc" in resp_lower:
        result_status = "CVV Fail"
        live = False
    elif "otp" in resp_lower or "3ds" in resp_lower or "action_required" in resp_lower:
        result_status = "OTP Required"
        live = False
    elif "captcha" in resp_lower:
        result_status = "Site Error"
        live = False
    elif status is True or "order_placed" in resp_lower or "approved" in resp_lower:
        result_status = "Charged"
        live = True
    else:
        result_status = response
        live = False


    return {
        "Response": response,
        "Price": price,
        "Gateway": gateway,
        "Status": result_status,
        "Live": live,
        "CC": raw.get("cc", cc_str),
        "Site": raw.get("Site", site_url),
    }


# ── Public API ─────────────────────────────────────────────────────────────

async def check_card_site(
    cc_str: str,
    site_url: str,
    proxy_data: dict | str | None,
    variant_id: str | None = None,
) -> dict:
    """
    Main entry point for bot.py to check a card against a Shopify site.

    Args:
        cc_str: Card details in format "CC|MM|YYYY|CVV"
        site_url: Shopify store domain or full URL
        proxy_data: Proxy dict or string
        variant_id: Optional specific variant ID

    Returns:
        dict with Response, Price, Gateway, Status, Live, CC, Site
    """
    _ensure_health_loop()

    proxy_str = _proxy_data_to_proxy_str(proxy_data)
    if not proxy_str:
        return {
            "Response": "No proxy configured",
            "Price": "-",
            "Gateway": "-",
            "Status": "Error",
            "Live": False,
            "CC": cc_str,
            "Site": site_url,
        }

    # Normalize site URL
    if site_url and not site_url.startswith(("http://", "https://")):
        site_url = f"https://{site_url}"
    site_url = site_url.rstrip("/")

    tried = set()
    last_err = "All nodes failed"
    t_start = time.monotonic()
    cc4 = cc_str.split('|')[0][-4:] if '|' in cc_str else cc_str[-4:]

    log.info(f"[bridge] check | cc=...{cc4} | site={site_url} | proxy={proxy_str[:40]}...")

    while True:
        node = _pick_node(exclude=tried)
        if node is None:
            break
        tried.add(node)
        try:
            raw = await _call_node(node, cc_str, proxy_str, site_url, variant_id)
            result = _map_result(raw, cc_str, site_url)

            log.info(
                f"[bridge] done | node={node} | {(time.monotonic()-t_start)*1000:.0f}ms"
                f" | status={result.get('Status')} | resp={result.get('Response','')[:60]}"
            )

            # Check if proxy is burned
            resp_l = result.get("Response", "").lower()
            if any(ind in resp_l for ind in _PROXY_BURNED_INDICATORS):
                return {
                    "Response": "Proxy burned - change proxy",
                    "Price": "-",
                    "Gateway": "Shopify",
                    "Status": "Error",
                    "Live": False,
                    "CC": cc_str,
                    "Site": site_url,
                }
            return result

        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
            log.warning(f"[lb] node {node} failed — {last_err}")
            if len(tried) >= len(NODES):
                break
            await asyncio.sleep(0.05)

    # All nodes failed
    err_l = last_err.lower()
    if any(ind in err_l for ind in _PROXY_BURNED_INDICATORS):
        final_resp = "Proxy burned - change proxy"
    else:
        final_resp = f"All nodes failed: {last_err}"

    return {
        "Response": final_resp,
        "Price": "-",
        "Gateway": "-",
        "Status": "Error",
        "Live": False,
        "CC": cc_str,
        "Site": site_url,
    }


async def test_site(
    site_url: str,
    proxy_data: dict | str | None,
    test_card: str = "4031630422575208|01|2030|280",
) -> dict:
    """Quick test of a site with a test card."""
    raw = await check_card_site(test_card, site_url, proxy_data)
    response_text = raw.get("Response", "")
    price = raw.get("Price", "-")
    status = "working"

    resp_l = response_text.lower()
    if any(ind in resp_l for ind in _PROXY_BURNED_INDICATORS):
        status = "proxy_dead"
    elif any(ind in resp_l for ind in (
        "captcha", "timeout", "dead", "404", "500",
        "all nodes failed", "no proxy",
    )):
        status = "dead"

    return {
        "status": status,
        "response": response_text,
        "site": site_url,
        "price": price,
    }


# ── Management functions ──────────────────────────────────────────────────

def get_all_nodes() -> list[str]:
    return list(NODES)


async def check_node_health(node: str) -> bool:
    try:
        sess = await _get_session()
        async with sess.get(
            f"{node}/",
            timeout=aiohttp.ClientTimeout(total=6, connect=4),
        ) as r:
            return r.status in (200, 404)
    except Exception:
        return False


def is_node_disabled(node: str) -> bool:
    return node in _disabled_nodes


def disable_node(node: str) -> None:
    _disabled_nodes.add(node)
    log.info(f"[api] node DISABLED: {node}")


def enable_node(node: str) -> None:
    _disabled_nodes.discard(node)
    if node in _state:
        _state[node]["healthy"] = True
        _state[node]["consec_fails"] = 0
    log.info(f"[api] node ENABLED: {node}")


def get_node_stats() -> dict:
    """Get current stats for all nodes."""
    return {
        url: {
            "in_flight": s["in_flight"],
            "healthy": s["healthy"],
            "avg_ms": round(s["avg_ms"], 1),
            "total_ok": s["total_ok"],
            "consec_fails": s["consec_fails"],
            "disabled": url in _disabled_nodes,
        }
        for url, s in _state.items()
    }
