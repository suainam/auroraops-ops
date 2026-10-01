#!/usr/bin/env python3
"""Pure analysis for the vps.ops.ipquality role.

Everything here is a side-effect-free function over already-collected text, so
the three-stage closed loop can be unit tested offline without a network, a
SOCKS5 pool, or the upstream script:

  Stage 1  detection     parse the official xykt/ipquality JSON into facts
  Stage 2  geo-drift     decide whether Google/YouTube treat the exit as China
  Stage 3  remediation   turn findings into a ranked, structured recommendation

The upstream script's own YouTube Premium markers are reused verbatim so this
role cannot drift from the tool it wraps. From `ip.sh`
(`MediaUnlockTest_YouTube_Premium`):

    isCN           = body contains "www.google.cn"
    isNotAvailable = body contains "Premium is not available in your country"
    region         = the "contentRegion":"XX" field
    isAvailable    = body contains "ad-free"

Standard library only. No network access, no file writes, no credentials.
"""

from __future__ import annotations

import json
import re
from typing import Any

# --- Upstream markers (ip.sh MediaUnlockTest_YouTube_Premium) ---------------
YT_CN_MARKER = "www.google.cn"
YT_NOT_AVAILABLE_MARKER = "Premium is not available in your country"
YT_AVAILABLE_MARKER = "ad-free"
YT_REGION_RE = re.compile(r'"contentRegion"\s*:\s*"([^"]*)"')

# Google serves an interstitial at /sorry/ and redirects to a regional host
# when it decides the source address is not entitled to the requested region.
GOOGLE_SORRY_PATH = "/sorry/"
GOOGLE_CN_HOSTS = ("google.cn", "google.com.hk", "google.com.tw")

# Databases that answer with a fraud/abuse score, and the range they use.
SCORE_SOURCES = (
    "IP2LOCATION",
    "SCAMALYTICS",
    "ABUSEIPDB",
    "IPQS",
    # ipapi reports a percentage string ("0.77%") rather than an integer.
    "ipapi",
    "DBIP",
)

# Usage verdicts that mean "this is a datacentre, not a home broadband line".
DATACENTER_USAGE = {"hosting", "datacenter", "data center", "business"}
# Verdicts that positively identify a consumer access line.
RESIDENTIAL_USAGE = {"isp", "line isp", "residential", "mobile"}

NO_DATA = {"null", "", "none", None}

# Remediation strategies, ordered by how much they disturb production.
STRATEGY_REROUTE = "reroute_fixed_clean_egress"
STRATEGY_SENTINEL = "ip_sentinel_upstream_appeal"
STRATEGY_REPLACE = "warp_or_residential_replacement"
STRATEGY_NONE = "none_required"
# Findings exist but the inventory offered no lever to act on. This must never
# collapse into STRATEGY_NONE: that value means "exit passed both checks", and
# reporting it for a failing exit tells the operator to do nothing about a
# measured problem. Measured on nat-hk216, where geo drift was positive while
# reroute_targets/sentinel/residential were all unset.
STRATEGY_UNDECIDED = "findings_no_remediation_context"

STRATEGY_ORDER = (STRATEGY_REROUTE, STRATEGY_SENTINEL, STRATEGY_REPLACE,
                  STRATEGY_UNDECIDED, STRATEGY_NONE)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def has_data(value: Any) -> bool:
    """A missing score must never be read as a perfect score."""
    return _text(value).lower() not in NO_DATA


def strip_ansi(raw: str) -> str:
    """Remove the escape sequences the upstream script interleaves.

    ``ip.sh -j`` emits three distinct forms and all of them must go:

    * cursor/colour CSI sequences -- ``ESC[2J``, ``ESC[31m``;
    * bare SGR sequences with no bracket -- ``ESC31m`` (observed in real output,
      and the reason a naive ``ESC[``-only filter still yields invalid JSON);
    * a stray ``ESC`` before ordinary text.

    Leftover control characters are then removed so the result is strictly
    valid JSON.
    """
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)
    text = re.sub(r"\x1b\][0-9;]*[A-Za-z]", "", text)
    # Bare SGR: ESC followed by digits/semicolons and a final letter, with no
    # intervening bracket. Restricted to a non-bracket first parameter so a
    # normal CSI sequence cannot be partially rewritten.
    text = re.sub(r"\x1b[0-9;]*[A-Za-z]", "", text)
    # Any remaining bare ESC (and other C0 controls except tab/newline).
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x1b]", "", text)


def parse_upstream_json(raw: str) -> dict[str, Any] | None:
    """Parse the official script's JSON output, tolerating its escape codes."""
    cleaned = strip_ansi(raw)
    start = cleaned.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(cleaned[start:])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


# --- Stage 1: detection -----------------------------------------------------

def _usage_votes(payload: dict[str, Any]) -> dict[str, str]:
    node = payload.get("Type") if isinstance(payload.get("Type"), dict) else {}
    usage = node.get("Usage") if isinstance(node.get("Usage"), dict) else {}
    return {str(k): _text(v).lower() for k, v in usage.items()}


def collect_scores(payload: dict[str, Any]) -> dict[str, float | None]:
    """Normalize each database's score to a 0-100 scale.

    ipapi reports a percentage string ("0.77%"); the rest report integers.
    Anything that is absent stays ``None`` so a caller cannot mistake an
    unanswered database for a clean one.
    """
    node = payload.get("Score") if isinstance(payload.get("Score"), dict) else {}
    scores: dict[str, float | None] = {}
    for source in SCORE_SOURCES:
        raw = node.get(source)
        if not has_data(raw):
            scores[source] = None
            continue
        text = _text(raw).rstrip("%")
        try:
            scores[source] = float(text)
        except ValueError:
            scores[source] = None
    return scores


def max_fraud_score(scores: dict[str, float | None]) -> float | None:
    """Worst answered score, ignoring databases that did not respond."""
    answered = [value for value in scores.values() if value is not None]
    return max(answered) if answered else None


def is_residential(payload: dict[str, Any]) -> bool | None:
    """Residential only when a consumer-access verdict exists and no DC verdict does.

    Returns ``None`` when the databases did not answer, so an unreachable
    lookup cannot be reported as a pass.
    """
    votes = _usage_votes(payload)
    if not any(has_data(value) for value in votes.values()):
        return None
    saw_residential = any(value in RESIDENTIAL_USAGE for value in votes.values())
    saw_datacenter = any(value in DATACENTER_USAGE for value in votes.values())
    if saw_datacenter:
        return False
    return True if saw_residential else None


def is_hosting(payload: dict[str, Any]) -> bool | None:
    """True when any database calls the exit a datacentre."""
    votes = _usage_votes(payload)
    if not any(has_data(value) for value in votes.values()):
        return None
    return any(value in DATACENTER_USAGE for value in votes.values())


def datacenter_votes(payload: dict[str, Any]) -> list[str]:
    votes = _usage_votes(payload)
    return sorted(source for source, value in votes.items() if value in DATACENTER_USAGE)


def residential_votes(payload: dict[str, Any]) -> list[str]:
    votes = _usage_votes(payload)
    return sorted(source for source, value in votes.items() if value in RESIDENTIAL_USAGE)


def parse_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce the upstream document to the facts this role asserts on."""
    head = payload.get("Head") if isinstance(payload.get("Head"), dict) else {}
    info = payload.get("Info") if isinstance(payload.get("Info"), dict) else {}
    scores = collect_scores(payload)
    return {
        "ip_masked": _text(head.get("IP")),
        "asn": _text(info.get("ASN")),
        "organization": _text(info.get("Organization")),
        "region": _text((info.get("Region") or {}).get("Code"))
        if isinstance(info.get("Region"), dict) else "",
        "geo_consistency": _text(info.get("Type")).lower(),
        "scores": scores,
        "max_fraud_score": max_fraud_score(scores),
        "is_residential": is_residential(payload),
        "is_hosting": is_hosting(payload),
        "datacenter_votes": datacenter_votes(payload),
        "residential_votes": residential_votes(payload),
    }


# --- Stage 2: geo-drift ("sent to China") detection ------------------------

def detect_youtube_premium(body: str) -> dict[str, Any]:
    """Classify a YouTube Premium response using the upstream markers.

    ``sent_to_china`` is the #101 signal: a non-CN exit whose Premium page is
    served as China, which makes Google treat a US or JP address as Chinese.
    """
    text = _text(body)
    if not text:
        return {"status": "unknown", "sent_to_china": False, "region": "",
                "markers": [], "reason": "empty response"}
    markers: list[str] = []
    if YT_CN_MARKER in text:
        markers.append(YT_CN_MARKER)
    if YT_NOT_AVAILABLE_MARKER in text:
        markers.append(YT_NOT_AVAILABLE_MARKER)
    if YT_AVAILABLE_MARKER in text:
        markers.append(YT_AVAILABLE_MARKER)
    match = YT_REGION_RE.search(text)
    region = match.group(1) if match else ""

    if YT_CN_MARKER in text:
        status, reason = "sent_to_china", "YouTube Premium page served the google.cn marker"
    elif YT_NOT_AVAILABLE_MARKER in text:
        status, reason = "premium_unavailable", "Premium is not offered for this region"
    elif YT_AVAILABLE_MARKER in text:
        status, reason = "available", "Premium page carries the ad-free marker"
    else:
        status, reason = "unknown", "no recognised Premium marker in the response"
    return {"status": status, "sent_to_china": YT_CN_MARKER in text,
            "region": region, "markers": markers, "reason": reason}


def detect_google_redirect(body: str, final_url: str = "", http_status: int | None = None) -> dict[str, Any]:
    """Classify a Google response for the /sorry/ interstitial and CN hosts."""
    text = _text(body)
    url = _text(final_url).lower()
    path_hit = GOOGLE_SORRY_PATH in url
    host_hit = any(host in url for host in GOOGLE_CN_HOSTS)
    # The interstitial body names the reason; treat it as equivalent evidence.
    body_hit = "unusual traffic" in text.lower() and GOOGLE_SORRY_PATH in text.lower()
    region = ""
    match = YT_REGION_RE.search(text)
    if match:
        region = match.group(1)
    if path_hit or host_hit or body_hit:
        status = "sent_to_china" if host_hit else "blocked_sorry"
        reason = ("Google redirected to a China-specific host" if host_hit
                  else "Google served the /sorry/ interstitial")
    elif http_status is not None and 200 <= http_status < 300:
        status, reason = "ok", "Google served the request without a geo interstitial"
    else:
        status, reason = "unknown", "no geo verdict could be derived"
    return {"status": status, "sent_to_china": bool(host_hit), "region": region,
            "path": GOOGLE_SORRY_PATH if path_hit else "", "reason": reason,
            "http_status": http_status}


def evaluate_geo_drift(youtube: dict[str, Any], google: dict[str, Any]) -> dict[str, Any]:
    """Combine both probes into one drift verdict.

    Drift is only asserted when a probe actually produced a verdict. Two
    ``unknown`` results mean "not measured", which is deliberately not the same
    as "clean" -- otherwise a failed probe would silently pass the gate.
    """
    measured = youtube.get("status") != "unknown" or google.get("status") != "unknown"
    sent = bool(youtube.get("sent_to_china")) or bool(google.get("sent_to_china"))
    sorry = google.get("status") == "blocked_sorry"
    if not measured:
        verdict, reason = "unknown", "neither geo probe returned a usable verdict"
    elif sent:
        verdict, reason = "sent_to_china", "a geo probe classified this exit as China"
    elif sorry:
        verdict, reason = "geo_interstitial", "Google served the /sorry/ interstitial"
    else:
        verdict, reason = "consistent", "geo probes agree with the expected region"
    return {"geo_drift": sent or sorry, "verdict": verdict, "reason": reason,
            "youtube": youtube, "google": google}


# --- Stage 3: remediation feedback ----------------------------------------

def build_remediation(detection: dict[str, Any], drift: dict[str, Any],
                      *, reroute_targets: list[str] | None = None,
                      sentinel_enabled: bool = False,
                      residential_available: bool = False) -> dict[str, Any]:
    """Turn findings into a ranked, structured recommendation.

    The role only ever *recommends*. It never edits routing, which is the
    boundary this role is held to.
    """
    reasons: list[str] = []
    actions: list[dict[str, Any]] = []
    targets = [t for t in (reroute_targets or []) if t]

    if detection.get("is_hosting"):
        reasons.append(
            "exit is classified as a datacentre by "
            f"{', '.join(detection.get('datacenter_votes') or ['an upstream database'])}"
        )
    if detection.get("is_residential") is False:
        reasons.append("no consumer-access (residential/ISP) verdict was returned")
    if drift.get("geo_drift"):
        reasons.append(f"geo drift: {drift.get('reason')}")

    if not reasons:
        return {
            "remediation_strategy": STRATEGY_NONE,
            "reasons": [],
            "actions": [],
            "requires_operator_decision": False,
            "note": "exit passed residential and geo-drift checks",
        }

    # Strategy A: move traffic to a known-good fixed egress. Least invasive to
    # the IP itself, and it is the option #101 already proved works.
    if targets:
        actions.append({
            "strategy": STRATEGY_REROUTE,
            "rank": 1,
            "action": f"pin the affected traffic to a verified clean egress ({', '.join(targets)})",
            "rationale": "keeps the current IP untouched and removes the bad exit from the path",
            "reversible": True,
        })
    # Strategy B: dispute the upstream geolocation classification (#118).
    if sentinel_enabled:
        actions.append({
            "strategy": STRATEGY_SENTINEL,
            "rank": 2 if targets else 1,
            "action": "file an IP-Sentinel correction with the upstream provider",
            "rationale": "corrects the provider-side classification that caused the drift",
            "reversible": True,
        })
    # Strategy C: last resort, replace the egress entirely.
    if residential_available:
        actions.append({
            "strategy": STRATEGY_REPLACE,
            "rank": len(actions) + 1,
            "action": "replace the exit with a residential or WARP-backed egress",
            "rationale": "the only option that changes the address itself",
            "reversible": False,
        })

    ordered = sorted(actions, key=lambda item: item["rank"])
    # Findings without any configured lever is its own state, not "nothing to
    # do". Reporting STRATEGY_NONE here would make a failing exit read as a
    # passing one, which is exactly the false-negative this role must not emit.
    if not ordered:
        return {
            "remediation_strategy": STRATEGY_UNDECIDED,
            "reasons": reasons,
            "actions": [],
            "requires_operator_decision": True,
            "note": (
                "advisory only; this role never rewrites routing. Findings were "
                "measured but the inventory supplied no remediation lever "
                "(reroute targets, IP-Sentinel, residential alternatives all "
                "unset), so no strategy could be ranked -- supply that context "
                "to get an actionable recommendation."
            ),
        }

    return {
        "remediation_strategy": ordered[0]["strategy"],
        "reasons": reasons,
        "actions": ordered,
        "requires_operator_decision": True,
        "note": "advisory only; this role never rewrites routing",
    }


def _as_float(value: Any) -> float | None:
    """Coerce a limit to float, tolerating the strings Ansible templates in.

    `ipquality_max_fraud_score | float` is applied in the role, but a value
    arriving from an inventory, a vault, or an extra-var can still be a string
    ("50", "50.0"). Comparing a float to a str raises TypeError, which would
    surface as an opaque task failure instead of a threshold verdict.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def evaluate(payload: dict[str, Any] | None, youtube: dict[str, Any],
             google: dict[str, Any], *, max_fraud_score_limit: float | None = None,
             require_residential: bool = False,
             reroute_targets: list[str] | None = None,
             sentinel_enabled: bool = False,
             residential_available: bool = False) -> dict[str, Any]:
    """Run all three stages and return one assertion-ready verdict."""
    raw_limit = max_fraud_score_limit
    max_fraud_score_limit = _as_float(max_fraud_score_limit)
    if payload is None:
        return {
            "parsed": False,
            "detection": {},
            "drift": {"geo_drift": False, "verdict": "unknown",
                      "reason": "upstream output could not be parsed"},
            "remediation": {"remediation_strategy": STRATEGY_NONE, "reasons": [],
                            "actions": [], "requires_operator_decision": False},
            "violations": ["upstream output could not be parsed"],
            "passed": False,
        }

    detection = parse_summary(payload)
    drift = evaluate_geo_drift(youtube, google)
    remediation = build_remediation(
        detection, drift,
        reroute_targets=reroute_targets,
        sentinel_enabled=sentinel_enabled,
        residential_available=residential_available,
    )

    violations: list[str] = []
    if require_residential and detection["is_residential"] is not True:
        if detection["is_residential"] is None:
            violations.append("residential check did not return a verdict")
        else:
            violations.append("exit is not a residential/ISP address")
    worst = detection["max_fraud_score"]
    if raw_limit is not None and max_fraud_score_limit is None:
        # A configured but unusable threshold must not quietly disable the
        # gate; that would turn a typo into a permanent pass.
        violations.append(
            f"fraud score limit is not a usable number: {raw_limit!r}"
        )
    elif max_fraud_score_limit is not None:
        if worst is None:
            violations.append("no fraud score was returned by any database")
        elif worst > max_fraud_score_limit:
            violations.append(
                f"fraud score {worst:g} exceeds the limit {max_fraud_score_limit:g}"
            )
    if drift["verdict"] == "sent_to_china":
        violations.append("exit is geo-classified as China (sent-to-China drift)")

    return {
        "parsed": True,
        "detection": detection,
        "drift": drift,
        "remediation": remediation,
        "violations": violations,
        "passed": not violations,
    }
