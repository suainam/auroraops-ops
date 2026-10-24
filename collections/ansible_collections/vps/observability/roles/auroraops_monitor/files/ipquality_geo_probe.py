#!/usr/bin/env python3
"""Stage-2 prober: does Google or YouTube treat this exit as China?

Runs on the measured host (directly, or through a SOCKS5 exit when one is
configured) and prints a single JSON object on stdout. It reuses the marker set
from the official xykt/ipquality script so the two never disagree about what
counts as "sent to China" (see ip.sh `MediaUnlockTest_YouTube_Premium` and the
#101 baseline that recorded `www.google.cn` / `Premium is not available`).

Only a bounded, non-sensitive excerpt of each body is kept: the markers and the
region field. Full page bodies are never emitted, because they can contain
account or session text.

Credentials, when a SOCKS5 exit is used, arrive in the environment so they stay
out of argv and out of `ps`.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

# Same cookies/headers the upstream script uses, so the response shape matches.
YOUTUBE_URL = "https://www.youtube.com/premium"
YOUTUBE_HEADERS = {
    "Accept-Language": "en",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}
GOOGLE_URL = "https://www.google.com/search?q=ipquality"

YT_CN_MARKER = "www.google.cn"
YT_NOT_AVAILABLE_MARKER = "Premium is not available in your country"
YT_AVAILABLE_MARKER = "ad-free"
REGION_RE = re.compile(r'"contentRegion"\s*:\s*"([^"]*)"')
SORRY_PATH = "/sorry/"

TIMEOUT = 20
# The Premium page is ~790 KB and the decisive markers sit near its end:
# "www.google.cn" was observed at byte ~688 KB and the "not available" string at
# ~771 KB. An earlier 256 KB cap therefore reported `unknown` on a genuinely
# drifted exit -- a false all-clear, which is the one failure mode this role must
# not have. The page is public HTML with no account data, so reading it whole is
# safe; the cap exists only to bound memory.
MAX_BYTES = 4194304


def build_opener(socks5: str) -> urllib.request.OpenerDirector:
    """Return an opener that tunnels through a SOCKS5 proxy, if requested.

    urllib has no SOCKS support, so this only works when a stdlib-only build
    cannot help: we report the reason instead of silently falling back to a
    direct request, which would measure the wrong egress.
    """
    if not socks5:
        return urllib.request.build_opener()
    try:
        from urllib.request import ProxyHandler  # noqa: F401
        import socks  # type: ignore
    except ImportError:
        return None
    handler = socks.ProxyHandler({"http": socks5, "https": socks5})
    return urllib.request.build_opener(handler)


def fetch(opener: urllib.request.OpenerDirector, url: str,
          headers: dict[str, str]) -> dict[str, object]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES).decode("utf-8", errors="replace")
            return {"ok": True, "status": response.status, "body": body,
                    "final_url": response.geturl()}
    except urllib.error.HTTPError as error:
        body = ""
        try:
            body = error.read(MAX_BYTES).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - body is best effort
            pass
        return {"ok": False, "status": error.code, "body": body,
                "final_url": getattr(error, "url", "") or ""}
    except Exception as error:  # noqa: BLE001 - report any transport failure
        return {"ok": False, "status": None, "body": "", "final_url": "",
                "error": type(error).__name__}


def probe_youtube(opener: urllib.request.OpenerDirector) -> dict[str, object]:
    result = fetch(opener, YOUTUBE_URL, YOUTUBE_HEADERS)
    body = str(result.get("body") or "")
    if not result.get("ok") and not body:
        return {"status": "unknown", "sent_to_china": False, "region": "",
                "markers": [], "reason": f"probe failed: {result.get('error', 'http error')}"}
    markers = [m for m in (YT_CN_MARKER, YT_NOT_AVAILABLE_MARKER, YT_AVAILABLE_MARKER)
               if m in body]
    match = REGION_RE.search(body)
    region = match.group(1) if match else ""
    if YT_CN_MARKER in body:
        status, reason = "sent_to_china", "YouTube Premium page served the google.cn marker"
    elif YT_NOT_AVAILABLE_MARKER in body:
        status, reason = "premium_unavailable", "Premium is not offered for this region"
    elif YT_AVAILABLE_MARKER in body:
        status, reason = "available", "Premium page carries the ad-free marker"
    else:
        status, reason = "unknown", "no recognised Premium marker in the response"
    return {"status": status, "sent_to_china": YT_CN_MARKER in body, "region": region,
            "markers": markers, "reason": reason, "http_status": result.get("status")}


def probe_google(opener: urllib.request.OpenerDirector) -> dict[str, object]:
    result = fetch(opener, GOOGLE_URL, {"User-Agent": YOUTUBE_HEADERS["User-Agent"]})
    body = str(result.get("body") or "")
    final_url = str(result.get("final_url") or "").lower()
    if not result.get("ok") and not body:
        return {"status": "unknown", "sent_to_china": False, "region": "",
                "path": "", "reason": f"probe failed: {result.get('error', 'http error')}"}
    path_hit = SORRY_PATH in final_url
    host_hit = any(host in final_url for host in ("google.cn", "google.com.hk", "google.com.tw"))
    match = REGION_RE.search(body)
    region = match.group(1) if match else ""
    if host_hit:
        status, reason = "sent_to_china", "Google redirected to a China-specific host"
    elif path_hit:
        status, reason = "blocked_sorry", "Google served the /sorry/ interstitial"
    elif result.get("ok"):
        status, reason = "ok", "Google served the request without a geo interstitial"
    else:
        status, reason = "unknown", "no geo verdict could be derived"
    return {"status": status, "sent_to_china": host_hit, "region": region,
            "path": SORRY_PATH if path_hit else "", "reason": reason,
            "http_status": result.get("status")}


def main() -> int:
    socks5 = os.environ.get("IPQUALITY_SOCKS5_URL", "")
    opener = build_opener(socks5)
    if opener is None:
        # Fail loudly rather than measure the host's own egress by accident.
        print(json.dumps({
            "error": "socks_unsupported",
            "detail": "stdlib urllib cannot reach a SOCKS5 exit; install PySocks "
                      "or run the probe where the exit is directly reachable",
        }))
        return 2
    print(json.dumps({
        "youtube": probe_youtube(opener),
        "google": probe_google(opener),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
