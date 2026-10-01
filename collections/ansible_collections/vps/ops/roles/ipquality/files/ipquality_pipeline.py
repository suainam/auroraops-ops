#!/usr/bin/env python3
"""Three-stage driver: detection -> geo-drift -> remediation.

Consumes a single JSON request on stdin and prints a single JSON verdict on
stdout, so the Ansible layer stays declarative and the whole decision path is
unit-testable offline.

Request shape (all keys optional except ``exits``):

    {
      "exits": [{"name": "self", "raw": "<upstream output>"}, ...],
      "geo_probes": {"self": {"youtube": {...}, "google": {...}}},
      "max_fraud_score": 50,
      "require_residential": true,
      "reroute_targets": ["nat-jp3"],
      "sentinel_enabled": false,
      "residential_available": true
    }

``exits`` may instead name an ``observations_dir`` written by the stage-1
runner. Reading the raw observations from disk keeps the large, escape-laden
upstream report out of Ansible's stdout, where it is fragile and needlessly
exposed in logs.

The driver never contacts the network; it only decides.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ipquality_analyzer import (  # noqa: E402
    build_remediation,
    evaluate,
    evaluate_geo_drift,
    parse_upstream_json,
)

UNKNOWN_GEO = {"status": "unknown", "sent_to_china": False, "region": "",
               "reason": "no geo probe was run for this exit"}


def coerce_number(value) -> float | None:
    """Accept the numeric strings Ansible produces from templated inventory."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def load_observations(directory: str) -> list[dict]:
    """Read stage-1 observation files written by ipquality_run_exit.py."""
    entries: list[dict] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            observation = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            entries.append({"name": path.stem, "raw": "",
                            "error": f"observation file is not valid JSON: {error}"})
            continue
        entries.append({
            "name": observation.get("exit_name", path.stem),
            "raw": observation.get("raw", ""),
            "error": observation.get("error", ""),
        })
    return entries


def run(request: dict) -> dict:
    limits = {
        # Coerced here as well as in evaluate(): the request crosses a JSON
        # boundary fed by Ansible, where a templated number arrives as a string.
        "max_fraud_score_limit": coerce_number(request.get("max_fraud_score")),
        "require_residential": bool(request.get("require_residential", False)),
        "reroute_targets": [str(t) for t in (request.get("reroute_targets") or []) if t],
        "sentinel_enabled": bool(request.get("sentinel_enabled", False)),
        "residential_available": bool(request.get("residential_available", False)),
    }
    probes = request.get("geo_probes") or {}
    exits = request.get("exits")
    if not exits and request.get("observations_dir"):
        exits = load_observations(request["observations_dir"])

    results = []
    for entry in exits or []:
        name = entry.get("name", "exit")
        raw = entry.get("raw", "")
        payload = parse_upstream_json(raw) if raw else None
        observed = probes.get(name) or {}
        youtube = observed.get("youtube") or dict(UNKNOWN_GEO)
        google = observed.get("google") or dict(UNKNOWN_GEO)

        if payload is None and entry.get("error"):
            # A failed observation still belongs in the report, otherwise a
            # broken exit silently disappears from the audit.
            #
            # The geo-drift verdict is evaluated anyway: the two stages are
            # independent, and stage 2 is pure stdlib. A host missing `jq`
            # cannot be scored, but it can still be shown to be sent to China,
            # and discarding that would hide the most actionable finding on
            # exactly the hosts most likely to need it.
            drift = evaluate_geo_drift(youtube, google)
            remediation = build_remediation(
                {"is_hosting": None, "is_residential": None, "datacenter_votes": []},
                drift,
                reroute_targets=limits["reroute_targets"],
                sentinel_enabled=limits["sentinel_enabled"],
                residential_available=limits["residential_available"],
            )
            results.append({
                "exit_name": name,
                "parsed": False,
                "detection": {},
                "drift": {**drift, "detection_error": entry["error"]},
                "remediation": remediation,
                "violations": [f"exit observation failed: {entry['error']}"],
                "passed": False,
            })
            continue

        verdict = evaluate(payload, youtube, google, **limits)
        verdict["exit_name"] = name
        results.append(verdict)

    return {
        "results": results,
        "summary": {
            "exit_count": len(results),
            "passed": sum(1 for item in results if item["passed"]),
            "failed": sum(1 for item in results if not item["passed"]),
            "geo_drift_exits": [item["exit_name"] for item in results
                                if item["drift"].get("geo_drift")],
            "unscored_exits": [item["exit_name"] for item in results
                               if not item.get("parsed")],
            "non_residential_exits": [item["exit_name"] for item in results
                                      if item["detection"].get("is_residential") is not True],
        },
    }


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(run(request), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
