# FTD Recovery

PXE/Clonezilla-based backup & restore for a fleet of x86 PCs, driven by a
FastAPI web UI running on a Raspberry Pi.

## One-line install

```bash
curl -fsSL https://YOUR-HOST/ftd-recovery/install.sh | sudo bash
```

Or, if cloning the repo:

```bash
git clone https://YOUR-HOST/ftd-recovery.git
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
| Helper scripts | `/usr/local/bin/recovery-{grubcfg,allowlist,rmimage}` | Privileged ops via sudo NOPASSWD |
| Sudoers | `/etc/sudoers.d/{ftd-grubcfg,ftd-rmimage,recovery-interface}` | Lets service user invoke helpers |

## Storage layout (chosen interactively at install)

The installer asks where backups should live and supports three modes:

1. **Use existing mount** — point at any writable path (e.g. `/mnt/backup`).
   Non-destructive, no fstab change to the underlying device.
2. **Adopt existing partition** — pick an already-formatted partition;
   installer reads its UUID, adds an fstab entry, mounts at `/mnt/ftd-backup`.
3. **Format fresh disk** — pick a whole disk; installer requires typing its
   serial number to confirm, then creates GPT + ext4 (label `ftd-backup`),
   adds fstab UUID entry, mounts.

In every case the installer ends by bind-mounting `<chosen>/clonezilla-images`
to `/srv/clonezilla-images`, which is the canonical app-facing path. App,
NFS export, and helper scripts all reference `/srv/clonezilla-images` only —
the physical disk under it is swappable without touching them.

## Verifying the install

```bash
systemctl status recovery-interface clonezilla-http recovery-dhcp-sniffer dnsmasq nfs-server
curl http://<server-ip>:8088/api/status
findmnt /srv/clonezilla-images
showmount -e localhost
```

## Uninstall

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
