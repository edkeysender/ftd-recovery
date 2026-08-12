# FTD Recovery

PXE/Clonezilla-based backup & restore for a fleet of x86 PCs, driven by a
FastAPI web UI running on a Raspberry Pi.

## One-line install

```bash
curl -fsSL http://gitlab.ftdinternal.aero/ftd-supp/ftd_recovery/-/raw/main/install.sh | sudo bash
```

Or, if cloning the repo:

```bash
git clone http://gitlab.ftdinternal.aero/ftd-supp/ftd_recovery.git
cd ftd_recovery
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
| Helper scripts | `/usr/local/bin/recovery-{grubcfg,allowlist,rmimage,remount,change-storage,update}` | Privileged ops via sudo NOPASSWD |
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

## Checking the installed version

```bash
curl -s http://<server-ip>:8088/api/version
# {"version":"1.0.0"}
```

## Updating an existing installation

### From the web UI (recommended)

Click **Update** in the header of the web interface. The Pi pulls the
configured ref from the internal GitLab and runs the updater; progress and the
log stream live in the dialog, and the interface reloads itself when done.

Devices **outside the company network** connect through the company VPN
(L2TP/IPsec) for the duration of the update and disconnect right after. The
dialog asks for the operator's VPN username/password — they are used once, held
in RAM only, and never stored. The tunnel is split: only GitLab's address is
routed through it, so PXE/NFS service on the local network is not interrupted.
Devices inside the company network leave the credential fields empty.

Per-device settings (VPN gateway, preshared key, GitLab IP, optional read-only
deploy token) live in `/etc/ftd-recovery/update.conf` (root-only, seeded from
`etc/update.conf.example` on first update).

### From the command line

Run on the Pi to pull the latest app, helpers, and sudoers from the repo:

```bash
curl -fsSL http://gitlab.ftdinternal.aero/ftd-supp/ftd_recovery/-/raw/main/update.sh | sudo bash
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
curl -fsSL http://gitlab.ftdinternal.aero/ftd-supp/ftd_recovery/-/raw/main/uninstall.sh | sudo bash
# or with storage purge:
curl -fsSL http://gitlab.ftdinternal.aero/ftd-supp/ftd_recovery/-/raw/main/uninstall.sh | sudo bash -s -- --purge-storage
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

The Pi's IP address is never baked into the installation. The web interface
binds all interfaces (`0.0.0.0:8088`), per-MAC PXE boot configs embed the
Pi's *current* address at the moment a job is armed (`recovery-grubcfg`
detects it from the PXE interface), and the `ocs-*.sh` client scripts read
the server address from their kernel cmdline at run time. A DHCP lease
change therefore cannot break the service or strand clients — the only
thing that moves is the browser URL. A static IP or DHCP reservation is
still convenient for a stable URL, but no longer required.

The **subnet** must stay the same after installation: the proxy-DHCP range
(`/etc/dnsmasq.d/clonezilla-pxe.conf`) and the NFS export (`/etc/exports`)
are bound to it. If the Pi moves to a different subnet, re-run the installer.
