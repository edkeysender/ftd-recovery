# FTD Recovery

PXE/Clonezilla-based backup & restore for a fleet of x86 PCs, driven by a
FastAPI web UI running on a Raspberry Pi.

## One-line install

```bash
curl -fsSL https://github.com/edkeysender/ftd-recovery/raw/main/install.sh | sudo bash
```

Or, if cloning the repo:

```bash
git clone https://github.com/edkeysender/ftd-recovery.git
cd ftd-recovery
sudo ./install.sh
```

Defaults: install prefix `/ftd/product/FTDRecovery`, service user `ftd`.
Override with `--prefix`, `--user`, `--interface`, `--server-ip`, `--subnet`.

## What gets installed

| Component | Path | Role |
|-----------|------|------|
| Web UI (FastAPI) | `<prefix>/app.py` + venv | Lists hosts, arm/disarm backup/restore, WoL |
| DHCP sniffer | `<prefix>/dhcp_namesniffer.py` | Passive hostname capture (DHCP opt 12 + NetBIOS) |
| Clonezilla HTTP | `clonezilla-http.service` (port 8080) | Serves squashfs + ocs scripts to PXE clients |
| TFTP + proxy-DHCP | dnsmasq | Hands PXE clients `grubnetx64.efi` |
| Boot chain | `/srv/tftp/{grubnetx64.efi, debian-installer/amd64/grub/grub.cfg, clonezilla/*}` | GRUB → Clonezilla live |
| NFS image store | `/srv/clonezilla-images` (bind) | Clients mount this to read/write images |
| Helper scripts | `/usr/local/bin/recovery-{grubcfg,allowlist,rmimage,remount,change-storage}` | Privileged ops via sudo NOPASSWD |
| Sudoers | `/etc/sudoers.d/{ftd-grubcfg,ftd-rmimage,recovery-interface}` | Lets service user invoke helpers |

## Storage layout (chosen interactively at install)

The installer asks where backups should live and supports two modes:

1. **Adopt existing partition** — pick an already-formatted partition;
   installer reads its UUID, adds an fstab entry, mounts at `/mnt/ftd-backup`.
2. **Format fresh disk** — pick a whole disk; requires typing `ERASE` to confirm,
   then creates GPT + ext4 (label `ftd-backup`), adds fstab UUID entry, mounts.

In every case the installer ends by bind-mounting `<chosen>/clonezilla-images`
to `/srv/clonezilla-images`, which is the canonical app-facing path. App,
NFS export, and helper scripts all reference `/srv/clonezilla-images` only —
the physical disk under it is swappable without touching them.

## Administration commands

### `recovery-change-storage`

Switch the backup storage drive without reinstalling:

```bash
sudo recovery-change-storage
```

Shows the current drive's path and usage, asks for confirmation, then walks you
through the same storage picker used by the installer. The old drive is
unmounted and its fstab entries are removed; any existing backup images on it
are left untouched. The `recovery-interface` service is restarted automatically
once the new drive is configured.

## Updating an existing installation

Run on each Pi to pull the latest app, helpers, and sudoers from the repo:

```bash
curl -fsSL https://github.com/edkeysender/ftd-recovery/raw/main/update.sh | sudo bash
```

The updater auto-detects the install prefix from the running service, installs
any newly required system packages, and restarts `recovery-interface`. It never
touches `hosts.yml`, `state.json`, fstab entries, network config, or backup data.

## Verifying the install

```bash
systemctl status recovery-interface clonezilla-http recovery-dhcp-sniffer dnsmasq nfs-server
curl http://<server-ip>:8088/api/status
findmnt /srv/clonezilla-images
showmount -e localhost
```

## Uninstall

One-line (same self-bootstrap as the installer):

```bash
curl -fsSL https://github.com/edkeysender/ftd-recovery/raw/main/uninstall.sh | sudo bash
# or with storage purge:
curl -fsSL https://github.com/edkeysender/ftd-recovery/raw/main/uninstall.sh | sudo bash -s -- --purge-storage
```

From a clone:

```bash
sudo ./uninstall.sh                    # keeps backup data and the bind mount
sudo ./uninstall.sh --purge-storage    # also removes the bind mount and fstab line
```

The underlying physical mount (`/mnt/ftd-backup` or whatever was chosen) and
its fstab entry are never removed automatically — strip them by hand if you
want to redeploy with a different storage choice.

## Network notes

The installer does **not** configure a static IP. Either set a DHCP
reservation for this Pi on your real DHCP server, or set a static IP via
NetworkManager / `/etc/dhcpcd.conf` / `/etc/network/interfaces` before
running the installer. Clients PXE-boot against the IP you give the
installer — if that changes, you must re-run the installer (or re-render
`/usr/local/bin/recovery-grubcfg` and the `ocs-*.sh` scripts by hand).
