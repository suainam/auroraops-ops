# vps.observability.auroraops_monitor

AuroraOps Instabox Outer Probe & Bounded Spool role.

## Features
- Bounded Spool (50MB maximum capacity) with single writer lock (`fcntl.flock`).
- Roll-based file rotation without active file truncation.
- Backpressure fail-closed mechanism: rejects new writes when spool capacity is exceeded with un-ACKed files. Never deletes un-ACKed data.
- Secure ACK handshake to remove only verified sealed files.
- Standards-compliant 9-stage lifecycle assurance.
