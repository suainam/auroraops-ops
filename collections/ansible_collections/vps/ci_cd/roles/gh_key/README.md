# Role: vps.ci_cd.gh_key

## 1. 概述

Configure GitHub Deploy Key for VPS to enable cloning and pulling private repositories.

### Features

- 🔐 Generate ED25519 SSH key pair
- 🤖 Automatically add Deploy Key via GitHub API
- 🔄 Idempotent operations (safe to run multiple times)
- 📝 Traceable key titles with hostname and timestamp
- ✅ Verify GitHub SSH connection
- 📦 Optional repository cloning

### Requirements

- Debian 12 (Bookworm)
- Ansible 2.9+
- Git installed (via `vps.system.init`)
- GitHub Personal Access Token (stored in Vaultwarden)

## 2. 变量说明

### Required Variables

```yaml
gh_key_repo: "owner/repo"                    # GitHub repository
gh_key_token: "{{ lookup('vps.services.vaultwarden', 'AuroraOps/GitHub/auroraops-pat', field='notes') }}"
```

### Optional Variables

```yaml
# SSH Key Configuration
gh_key_path: "~/.ssh/github_ed25519"         # Key file path
gh_key_type: "ed25519"                        # Key type (ed25519/rsa)
gh_key_comment: "{{ ansible_user }}@{{ ansible_hostname }}.{{ inventory_hostname }}"
gh_key_read_only: false                       # Read-only mode (false = allow push)

# SSH Config
gh_key_ssh_config_path: "~/.ssh/config"      # SSH config file path
gh_key_ssh_host: "github.com"                # GitHub hostname
gh_key_ssh_user: "git"                       # GitHub SSH user

# Repository Cloning
gh_key_clone_repo: true                      # Clone repository
gh_key_clone_dest: "~/AuroraOps"             # Clone destination
gh_key_clone_version: "main"                 # Branch to clone

# GitHub API
gh_key_api_base_url: "https://api.github.com"
gh_key_api_timeout: 30                       # API timeout (seconds)
```

## Dependencies

- `vps.system.init` (soft dependency for git installation)

## Example Playbook

### Basic Usage

```yaml
- hosts: all
  roles:
    - vps.ci_cd.gh_key
  vars:
    gh_key_repo: "suainam/AuroraOps"
    gh_key_token: "{{ lookup('vps.services.vaultwarden', 'AuroraOps/GitHub/auroraops-pat', field='notes') }}"
```

### Custom Configuration

```yaml
- hosts: all
  roles:
    - vps.ci_cd.gh_key
  vars:
    gh_key_repo: "myorg/myrepo"
    gh_key_token: "{{ vault_github_token }}"
    gh_key_path: "~/.ssh/myrepo_key"
    gh_key_read_only: true
    gh_key_clone_repo: false
```

### Key Setup Only (No Cloning)

```yaml
- hosts: all
  roles:
    - vps.ci_cd.gh_key
  vars:
    gh_key_repo: "suainam/AuroraOps"
    gh_key_token: "{{ vault_github_token }}"
    gh_key_clone_repo: false
```

## Usage

### Deploy

```bash
# Full deployment
make deploy-ci_cd.gh_key

# Specific sub-tags
make deploy-ci_cd.gh_key.gh_key_install    # Generate key
make deploy-ci_cd.gh_key.gh_key_config     # Configure SSH
make deploy-ci_cd.gh_key.gh_key_api        # Add to GitHub
make deploy-ci_cd.gh_key.gh_key_clone      # Clone repository
```

### Verify

```bash
make verify-ci_cd.gh_key
```

### Rollback

```bash
make rollback-ci_cd.gh_key
```

## Tags

All tasks use the following tag structure:

```yaml
tags: [action, domain, role, sub-tag, phase]
```

### Available Sub-Tags

- `gh_key_detect` - Detect existing configuration
- `gh_key_install` - Generate SSH key
- `gh_key_config` - Configure SSH
- `gh_key_api` - Add Deploy Key to GitHub
- `gh_key_verify` - Verify connection
- `gh_key_clone` - Clone repository
- `gh_key_rollback` - Rollback changes

## Security Considerations

- ✅ Uses ED25519 (more secure than RSA)
- ✅ Private key permissions: 600 (owner read/write only)
- ✅ GitHub Token uses `no_log: true`
- ✅ Token stored in Vaultwarden
- ✅ Deploy Key scoped to specific repository
- ✅ Supports read-only mode

## 3. 内部逻辑

### Workflow

1. **Detect Phase**: Check if GitHub SSH connection already works
   - Test SSH connection to github.com
   - Check if SSH key file exists
   - Skip setup if connection is successful

2. **Generate Phase**: Create ED25519 SSH key pair
   - Generate key with comment: `user@hostname.inventory_hostname`
   - Set correct permissions (600 for private, 644 for public)
   - Display public key for manual verification

3. **Configure Phase**: Update SSH config
   - Add GitHub host configuration to `~/.ssh/config`
   - Use `blockinfile` for idempotent updates
   - Configure `IdentitiesOnly yes` to avoid key conflicts

4. **API Phase**: Add Deploy Key to GitHub
   - Read public key content
   - Check if key already exists (by hostname in title)
   - Add via GitHub API if not present
   - Support both read-only and read-write modes

5. **Verify Phase**: Test connection and clone
   - Test SSH connection to GitHub
   - Assert successful authentication
   - Optionally clone repository

6. **Rollback Phase**: Remove configuration
   - Remove SSH config block
   - Delete SSH key files
   - Note: Deploy Key on GitHub must be removed manually

### Idempotency

The role is fully idempotent:

1. Detects existing GitHub SSH connection
2. Skips key generation if key exists
3. Skips API call if Deploy Key already exists
4. Safe to run multiple times

## 4. 依赖关系

### Meta Dependencies

```yaml
dependencies:
  - role: vps.system.init
    vars:
      init_setup: false  # Only ensure git is installed
```

### Runtime Dependencies

- **Git**: Required for cloning repositories (installed by `vps.system.init`)
- **OpenSSH Client**: Required for SSH key generation and connection testing
- **Vaultwarden**: Required for GitHub token retrieval (via lookup plugin)

### Phase Compatibility

- **Phase**: 10 (CI/CD)
- **Can depend on**: Any role in phase ≤ 10
- **Safe dependencies**: `vps.system.init` (phase 0)

## 5. 维护与排查

### Troubleshooting

### Common Issues

#### Connection Test Failed

```bash
# Manual test
ssh -T git@github.com

# Check SSH config
cat ~/.ssh/config

# Check key permissions
ls -la ~/.ssh/github_ed25519*
```

#### Deploy Key Not Added

- Verify GitHub token has `repo` scope
- Check token in Vaultwarden: `AuroraOps/GitHub/auroraops-pat`
- Manually add key from GitHub UI if API fails

#### Clone Failed

- Ensure Deploy Key has write access (`gh_key_read_only: false`)
- Verify repository path is correct
- Check SSH connection first

### Maintenance Tasks

#### Rotate SSH Key

```bash
# 1. Remove old key
make rollback-ci_cd.gh_key

# 2. Generate new key
make deploy-ci_cd.gh_key

# 3. Verify new key
make verify-ci_cd.gh_key
```

#### Update Deploy Key Permissions

1. Go to GitHub repository settings
2. Navigate to Deploy Keys
3. Find key by hostname in title
4. Toggle "Allow write access" checkbox

#### Check Key Status

```bash
# Test connection
ssh -T git@github.com

# List Deploy Keys via API
curl -H "Authorization: token YOUR_TOKEN" \
  https://api.github.com/repos/OWNER/REPO/keys
```

### Monitoring

- **Connection Status**: Run `make verify-ci_cd.gh_key` periodically
- **Key Expiration**: Deploy Keys don't expire, but monitor for unauthorized access
- **API Rate Limits**: GitHub API has rate limits (5000 requests/hour for authenticated users)

## License

MIT

## Author

AuroraOps Team
