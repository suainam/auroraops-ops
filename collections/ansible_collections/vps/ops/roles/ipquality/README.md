# vps.ops.ipquality

Read-only egress IP quality audit for AuroraOps.

It answers three questions about an exit, and nothing else:

1. **Is this a real home connection?** Runs the official
   [xykt/ipquality](https://github.com/xykt/ipquality) scoring script and reduces
   the multi-database verdicts to a residential / datacentre decision.
2. **Does Google or YouTube treat it as China?** Probes YouTube Premium and
   Google for the "sent to China" geo drift recorded in issue #101, where a US
   or JP address is served as CN.
3. **What should an operator do about it?** Emits a ranked, structured
   remediation recommendation. The role never applies it.

## Scope and boundary

This role is **advisory and read-only**. It observes egress and writes a report;
it never changes routing, never manages a service, and never edits a business
exit configuration. Acting on a recommendation is an explicit operator or
orchestration decision — see issue #118, where a trial was kept out of the Role
Catalog on purpose.

Per ADR-0001, diagnostics and audit capability belongs in `auroraops-ops`.

## What it writes

Only `ipquality_report_dir` (default `/var/lib/ipquality`):

- `report.json` — the full three-stage verdict;
- `report.json.md` — a copy for quick reading;
- `exits/<name>.json` — per-exit raw observations.

Rollback deletes that directory and nothing else; it refuses any path outside
`/var/lib/ipquality`.

The upstream script is fetched to a temporary directory and the bundled helpers
run through the `script` module, so no resident state is left on the host.

## Usage

```yaml
ipquality_enabled: true
ipquality_require_residential: true
ipquality_max_fraud_score: 50

ipquality_exits:
  - name: self
    direct: true
  - name: jp
    country: JP
    socks5:
      server: jp.example.net
      port: 40002
      username: "{{ vault_resi_jp_user }}"
      password: "{{ vault_resi_jp_pass }}"
```

`direct: true` measures the host's own public egress. Anything else is measured
through the given SOCKS5 exit. Credentials are read from the environment inside
the task, never from `argv`, and every task that touches them sets
`no_log: true`.

### Gate and advisory modes

`ipquality_fail_on_violation: false` records findings without failing the run —
useful for a first survey. Left `true`, a breach fails the task loudly.

## Remediation strategies

| Strategy | Meaning | Reversible |
| --- | --- | --- |
| `reroute_fixed_clean_egress` | Pin affected traffic to a verified clean exit (the approach issue #101 proved) | yes |
| `ip_sentinel_upstream_appeal` | Dispute the provider-side geolocation classification (issue #118) | yes |
| `warp_or_residential_replacement` | Replace the exit itself | no |
| `none_required` | Exit passed both checks | n/a |

Strategies are ranked by how little they disturb production, and the set is
driven by what the inventory actually offers: reroute targets, an enabled
IP-Sentinel, and available residential alternatives.

## Interpretation notes

These are deliberate, and they are what keep the role from lying:

- **A missing database is never a clean score.** If no database answers, the run
  fails the gate rather than passing on absent evidence.
- **A datacenter verdict beats a company `isp` label.** Real hosting providers
  report `company.type = isp` while the ASN type is `hosting`.
- **"Not measured" is not "clean".** If both geo probes fail to return a
  verdict, the drift state is `unknown`, never `consistent`.
- **Fraud score and residential attribute are independent.** A datacentre can
  score near zero because it has not been abused yet. A low score does not make
  an address residential.

## Lifecycle

| Stage | File | Mutation |
| --- | --- | --- |
| deploy / check / idempotence | `tasks/main.yml` | report only |
| verify | `tasks/verify.yml` | none — re-reads the stored report |
| rollback | `tasks/rollback.yml` | removes the report directory |
