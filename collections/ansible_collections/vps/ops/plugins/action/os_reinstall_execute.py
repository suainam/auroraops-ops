"""Ansible Action Plugin for indivisible safe execution of vps.ops.os_reinstall."""

from __future__ import annotations

import os
import re
import sys
from typing import Any

from ansible.errors import AnsiblePromptNoninteractive
from ansible.module_utils.common.text.converters import to_text
from ansible.plugins.action import ActionBase

PINNED_SCRIPT_COMMIT = "2252ca7615cd672dc1d9e67c52243db4ac859151"
PINNED_SCRIPT_SHA256 = "84daf5f1220ebdaf8e2d3db780e5f6b5d3a4d66a1fa70b353731662cd19a0010"

PUBKEY_REGEX = re.compile(
    r"^(ssh-(ed25519|rsa|dss)|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9-]+@openssh[.]com)"
    r"[ \t]+[A-Za-z0-9+/=]+([ \t]+.*)?$"
)


class ActionModule(ActionBase):
    def run(self, tmp: Any = None, task_vars: dict[str, Any] | None = None) -> dict[str, Any]:
        if task_vars is None:
            task_vars = {}
        result = super().run(tmp, task_vars)

        # 1. Authoritative check mode guard
        if self._task.check_mode:
            result["failed"] = True
            result["msg"] = (
                "[CHECK MODE REJECTED] vps.ops.os_reinstall is a destructive operational role "
                "and cannot be run in check mode."
            )
            return result

        # 2. Guard single host scope
        hosts = task_vars.get("ansible_play_hosts_all", [])
        if len(hosts) != 1:
            result["failed"] = True
            result["msg"] = (
                f"[SCOPE REJECTED] vps.ops.os_reinstall 严禁并发或批量执行。ansible_play_hosts_all "
                f"必须且仅能包含 1 台主机，当前为 {len(hosts)} 台 ({', '.join(hosts)})。"
            )
            return result

        target_host = task_vars.get("inventory_hostname", "localhost")

        # 3. Explicit operator backup prerequisite acknowledgement
        ack = task_vars.get("os_reinstall_backup_operator_ack", "")
        if not isinstance(ack, str) or ack.strip() != target_host:
            result["failed"] = True
            result["msg"] = (
                f"[BACKUP PREREQUISITE] 目标主机 '{target_host}' 即将被彻底格式化重装。\n"
                f"必须显式传递 -e os_reinstall_backup_operator_ack='{target_host}'，"
                "作为操作员对该主机外部备份已就绪、具备控制台/异地恢复手段的责任声明。"
            )
            return result

        # 4. Mandatory fresh interactive TTY prompt
        prompt_str = (
            f"\n[CRITICAL WARNING] Bare-metal OS reinstallation will permanently destroy all data on '{target_host}'.\n"
            f"To confirm, type the exact hostname: "
        )
        user_input = None
        try:
            with open("/dev/tty", "r+", buffering=1) as tty:
                if not os.isatty(tty.fileno()):
                    result["failed"] = True
                    result["msg"] = "[TTY REJECTED] /dev/tty is not an interactive terminal."
                    return result
                tty.write(prompt_str)
                tty.flush()
                user_input = tty.readline().strip()
        except OSError as tty_err:
            try:
                raw_input = self._display.prompt_until(prompt_str, private=False)
                user_input = to_text(raw_input).strip()
            except AnsiblePromptNoninteractive:
                result["failed"] = True
                result["msg"] = (
                    f"[TTY REJECTED] No interactive controlling terminal (/dev/tty: {tty_err}). "
                    "Interactive operator confirmation is strictly required."
                )
                return result

        if user_input != target_host:
            result["failed"] = True
            result["msg"] = (
                f"[CONFIRMATION REJECTED] Confirmation input '{user_input}' "
                f"does not match current target host '{target_host}'."
            )
            return result

        # Dispatch action
        action = self._task.args.get("action")
        if action == "download":
            return self._handle_download(task_vars)
        elif action == "prepare":
            return self._handle_prepare(task_vars)
        elif action == "reboot":
            return self._handle_reboot(task_vars)
        else:
            result["failed"] = True
            result["msg"] = f"Unknown action: {action}"
            return result

    def _handle_download(self, task_vars: dict[str, Any]) -> dict[str, Any]:
        script_url = self._task.args.get(
            "script_url",
            f"https://cnb.cool/bin456789/reinstall/-/git/raw/{PINNED_SCRIPT_COMMIT}/reinstall.sh",
        )
        script_path = self._task.args.get("script_path", "/root/reinstall.sh")
        # Enforce pinned checksum directly in action
        return self._execute_module(
            module_name="ansible.builtin.get_url",
            module_args={
                "url": script_url,
                "dest": script_path,
                "checksum": f"sha256:{PINNED_SCRIPT_SHA256}",
                "mode": "0755",
                "timeout": 30,
            },
            task_vars=task_vars,
        )

    def _handle_prepare(self, task_vars: dict[str, Any]) -> dict[str, Any]:
        script_path = self._task.args.get("script_path", "/root/reinstall.sh")
        # 1. Assert local script existence and exact pinned checksum before execution
        stat_res = self._execute_module(
            module_name="ansible.builtin.stat",
            module_args={"path": script_path, "checksum_algorithm": "sha256"},
            task_vars=task_vars,
        )
        file_stat = stat_res.get("stat", {})
        if not file_stat.get("exists", False):
            return {
                "failed": True,
                "msg": f"[CHECKSUM FAILURE] Script file does not exist: {script_path}",
            }
        observed_checksum = file_stat.get("checksum", "")
        if observed_checksum != PINNED_SCRIPT_SHA256:
            return {
                "failed": True,
                "msg": (
                    f"[CHECKSUM FAILURE] Script {script_path} checksum mismatch! "
                    f"Expected: {PINNED_SCRIPT_SHA256}, Observed: {observed_checksum}. "
                    "Aborting execution immediately."
                ),
            }

        # 2. Parameter validation
        debian_version = str(self._task.args.get("debian_version", "13")).strip()
        if debian_version not in ["12", "13"]:
            return {"failed": True, "msg": f"Debian version must be 12 or 13, got {debian_version}"}

        ssh_pubkey = str(self._task.args.get("ssh_pubkey", "")).strip()
        if not PUBKEY_REGEX.match(ssh_pubkey):
            return {"failed": True, "msg": "Invalid or empty SSH public key format."}

        ssh_port = int(self._task.args.get("ssh_port", 22))
        if not (1 <= ssh_port <= 65535):
            return {"failed": True, "msg": f"Invalid SSH port: {ssh_port}"}

        username = str(self._task.args.get("username", "root")).strip()
        if not username:
            return {"failed": True, "msg": "Username cannot be empty."}

        # 3. Indivisibly execute command
        cmd_args = [
            script_path,
            "debian",
            debian_version,
            "--ssh-key",
            ssh_pubkey,
            "--ssh-port",
            str(ssh_port),
            "--username",
            username,
        ]
        return self._execute_module(
            module_name="ansible.builtin.command",
            module_args={"argv": cmd_args},
            task_vars=task_vars,
        )

    def _handle_reboot(self, task_vars: dict[str, Any]) -> dict[str, Any]:
        return self._execute_module(
            module_name="ansible.builtin.command",
            module_args={"argv": ["shutdown", "-r", "now"]},
            task_vars=task_vars,
        )
