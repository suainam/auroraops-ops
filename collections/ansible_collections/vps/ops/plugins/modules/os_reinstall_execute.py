#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import annotations

DOCUMENTATION = r"""
---
module: os_reinstall_execute
short_description: Indivisible execution engine for bare-metal OS reinstallation
description:
  - Executes download, prepare, or reboot steps for vps.ops.os_reinstall.
  - Enforces check mode prohibition, single-host scope, operator backup acknowledgement,
    interactive TTY hostname confirmation, and SHA256 integrity check indivisibly inside the action plugin.
options:
  action:
    description: Action to perform.
    type: str
    choices: [download, prepare, reboot]
    required: true
  script_url:
    description: URL to fetch reinstall.sh from.
    type: str
  script_path:
    description: Path to local cached reinstall.sh.
    type: str
  debian_version:
    description: Target Debian major version.
    type: str
    choices: ['12', '13']
  ssh_pubkey:
    description: SSH public key content.
    type: str
  ssh_port:
    description: SSH port.
    type: int
  username:
    description: Target admin username.
    type: str
"""

EXAMPLES = r"""
- name: Download reinstall script
  vps.ops.os_reinstall_execute:
    action: download

- name: Prepare reinstall
  vps.ops.os_reinstall_execute:
    action: prepare
    script_path: /root/reinstall.sh
    debian_version: "13"
    ssh_pubkey: "ssh-ed25519 AAAAC3..."
    ssh_port: 22
    username: "root"

- name: Trigger reboot
  vps.ops.os_reinstall_execute:
    action: reboot
"""

RETURN = r"""
changed:
  description: Whether any remote modification was performed.
  type: bool
  returned: always
"""
