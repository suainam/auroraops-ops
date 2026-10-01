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
| `findings_no_remediation_context` | Problems were measured, but the inventory offered no lever to act on | n/a |
| `none_required` | Exit passed both checks | n/a |

Strategies are ranked by how little they disturb production, and the set is
driven by what the inventory actually offers: reroute targets, an enabled
IP-Sentinel, and available residential alternatives. When it offers none, the
result is `findings_no_remediation_context` rather than `none_required` — see
the pitfalls section.

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

## Pitfalls and defenses

Every item here is a defect that actually shipped and then produced a wrong or
unusable answer. They are recorded because each one fails *silently*.

### A truncated probe reports a drifted exit as clean

The YouTube Premium page is roughly 790 KB and the decisive markers sit near its
end: `www.google.cn` was measured at byte ~688 KB and
`Premium is not available in your country` at ~771 KB. An earlier 256 KB read cap
truncated the body before either marker, so a genuinely drifted exit came back
`unknown` — a false all-clear, which is the single worst failure this role can
have.

`MAX_BYTES` in `files/ipquality_geo_probe.py` is therefore 4 MiB. The page is
public HTML with no account data, so reading it whole is safe; the cap exists
only to bound memory. **If you raise the marker threshold, raise the cap too,
and re-measure where the marker actually lands.**

### Bare SGR sequences from upstream break JSON parsing

The upstream `ip.sh` emits ANSI colour, including escape sequences with **no
bracketed parameter list** — `ESC 31 m`, not `ESC [ 31 m`. A single regex written
for the well-formed form leaves those bytes in the string and `json.loads` dies
deep inside a task, with the real cause buried under an opaque parse error.

`strip_ansi()` in `files/ipquality_run_exit.py` cleans in three layers, because
one pass cannot be exhaustive:

1. `ESC [ … letter` — CSI sequences with parameters;
2. `ESC ] … letter` — OSC sequences;
3. `ESC [0-9;]* letter` — the bare-SGR form, then a final sweep of raw control
   characters.

A surviving escape byte is not cosmetic: it invalidates the JSON document and
the audit returns no verdict at all.

### macOS `curl` silently ignores `-x socks5`

The system `curl` on macOS is built without SOCKS support. It does not error on
`-x socks5://…`; it **drops the option and connects directly**, so the "measured
exit" is really the control plane's own residential broadband. The audit then
reports the controller's public IP as the exit under test — a completely wrong
answer with a clean exit code.

Two defenses, both mandatory:

- The Python probe uses `socks.ProxyHandler` and reports `socks_unsupported`
  explicitly rather than falling back to a direct request.
- **Always assert that the observed egress IP equals the intended exit IP.**
  Do not trust the label on an exit. `socks5h` (not `socks5`) is used so DNS
  resolves on the exit side.

### `curl` in the environment silently hijacks the measurement

An ambient `http_proxy` / `https_proxy` / `all_proxy` on the target reroutes the
exit-IP lookup and reports the host's own address as the measured exit. Every
probe task therefore clears all six proxy variables explicitly, in both cases.

### Jinja's `default()` does not rescue an undefined attribute

`ipquality_workdir.path | default('')` does **not** produce `''` when `path` is
undefined on a dict — it stringifies the object. The boolean second argument is
required: `| default('', true)`. Same class of bug in the `--check` workdir
derivation, where `set_fact` is not persisted at all.

### A threshold must be coerced, and a bad one must fail loudly

`ipquality_max_fraud_score` is asserted numeric (`0 <= x <= 100`) at the top of
`tasks/main.yml` and coerced with `| float` before it reaches the analyzer. The
analyzer additionally runs `_as_float()`, because a value arriving from
inventory, a vault or an extra-var can still be a string; comparing a float to a
str raises `TypeError` and surfaces as an opaque task failure instead of a
threshold verdict.

An illegal value **raises**. It never degrades to "no limit", because a typo
that silently becomes unlimited is a permanent allow.

### Findings with no configured lever are not "nothing to do"

Measured on `nat-hk216`: geo drift was positive while reroute targets,
IP-Sentinel and residential alternatives were all unset, so no action could be
ranked and the old fallback reported `none_required` — documented as "exit
passed both checks". A failing exit was therefore presented as needing no
remediation.

Findings without a lever now report `findings_no_remediation_context`, and a
genuinely clean exit still reports `none_required`.

### The `--check` gate cannot persist anything

`--check` does not execute `tempfile`, does not persist `set_fact`, and does not
create directories. A work directory derived from a registered `tempfile` result
therefore exists in preview and vanishes in the real run. `ipquality_workdir` is
a plain variable for exactly this reason, and every staging/download/probe task
is guarded with `not ansible_check_mode` so the idempotence gate — a repeated
`--check` — reports `changed=0` instead of failing on a missing helper.

### Dotted `-e` overrides silently no-op under `hash_behaviour=replace`

Passing `-e auroraops_roles.ops.ipquality=true` looks correct and does nothing:
the inventory defines `auroraops_roles` as a whole dict, so a dotted extra-var
does not merge into it. Every task then skips and the play reports
`ok=0 skipped=8` — which reads like a pass.

Pass the gate as a JSON extra-vars file instead:

```bash
-e @/path/to/ipquality.json   # {"auroraops_roles": {"ops": {"ipquality": true}}, ...}
```

**Assert the gate resolves before trusting a green recap.** A role that is
silently disabled looks exactly like an idempotent role.

## Lifecycle

| Stage | File | Mutation |
| --- | --- | --- |
| preflight | `tasks/preflight.yml` | none — probes only |
| deploy / check / idempotence | `tasks/main.yml` | report only |
| verify | `tasks/verify.yml` | none — re-reads the stored report |
| rollback | `tasks/rollback.yml` | removes the report directory |
| rollback_verify | `tasks/rollback_verify.yml` | none — asserts the baseline |

## Running the lifecycle on a non-pristine host

Certified on `nat-hk216` (Alpine 3.24, OpenRC, 7.9 GiB, `jq` absent) — a real
NAT node already serving nginx and sing-box, not a clean VM.

**Enable it.** Gate and configuration come from a JSON extra-vars file (see the
`hash_behaviour` pitfall above):

```bash
make switch_remote.nat-hk216
make preflight-ops.ipquality  ANSIBLE_EXTRA_ARGS="-e @ipquality.json"
make check-ops.ipquality      ANSIBLE_EXTRA_ARGS="-e @ipquality.json"   # changed=0
make deploy-ops.ipquality     ANSIBLE_EXTRA_ARGS="-e @ipquality.json"
make verify-ops.ipquality     ANSIBLE_EXTRA_ARGS="-e @ipquality.json"
make idempotence-ops.ipquality ANSIBLE_EXTRA_ARGS="-e @ipquality.json"  # changed=0
make rollback-ops.ipquality   ANSIBLE_EXTRA_ARGS="-e @ipquality.json"
make rollback_verify-ops.ipquality ANSIBLE_EXTRA_ARGS="-e @ipquality.json"
make redeploy-ops.ipquality   ANSIBLE_EXTRA_ARGS="-e @ipquality.json"
make recovery_verify-ops.ipquality ANSIBLE_EXTRA_ARGS="-e @ipquality.json"
```

Set `ipquality_protected_services: [nginx, sing-box]` (OpenRC) so
`rollback_verify` can prove the audit disturbed nothing. On a systemd host use
`systemctl is-active` semantics instead.

**Capture a baseline first.** Record the OS, interpreter, `jq` presence, memory,
egress IP, whether `/var/lib/ipquality` exists, and the running services plus
their listening ports. Store it outside the repo with no credentials in it.

**A missing `jq` is not fatal.** The upstream script needs it, but stage 2 is
pure stdlib and still works. `preflight` warns rather than aborting, and stage 1
records `detection_error` instead of silently reporting a clean score. Never let
a missing database read as "no risk found".

**Read-only contract, asserted not assumed:**

- every task touching credentials sets `no_log: true`;
- SOCKS5 credentials arrive via the environment, never `argv`, so they stay out
  of `ps` and out of the log;
- the role writes only under `ipquality_report_dir` — no routing, no iptables,
  no service, no proxy configuration;
- `rollback_verify` independently asserts the report directory is gone, no
  staged helper survived, and every declared protected service is still running.

**Expect transient SSH failures.** A `rollback_verify` run can report
`unreachable=1` from a dropped ControlMaster socket. That is the transport, not
the role — re-run before concluding anything.

### Interpreting a real result

`nat-hk216` measured as `geo=sent_to_china`, with `residential=unknown` because
`jq` was absent so no database could be scored. That is the honest answer: the
geo drift is measured fact, the residential attribute is genuinely unknown, and
the report says both rather than collapsing them into one verdict.
