#!/usr/bin/env bash
# FTD Recovery uninstaller.
# Removes services, configs, and app files. Backup data is preserved by default.
# Pass --purge-storage to also unmount and remove the bind mount + fstab entries.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
require_root

PURGE_STORAGE=0
INSTALL_PREFIX="/ftd/product/FTDRecovery"
SERVICE_USER="ftd"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge-storage) PURGE_STORAGE=1; shift ;;
        --prefix)        INSTALL_PREFIX="$2"; shift 2 ;;
        --user)          SERVICE_USER="$2"; shift 2 ;;
        *) die "unknown arg: $1" ;;
    esac
done

confirm "Remove FTD Recovery from this host?" "n" || exit 0

log "stopping services"
for s in recovery-interface recovery-dhcp-sniffer clonezilla-http; do
    systemctl disable --now "$s" 2>/dev/null || true
    rm -f "/etc/systemd/system/$s.service"
done
systemctl daemon-reload

log "removing helpers and sudoers"
rm -f /usr/local/bin/recovery-grubcfg /usr/local/bin/recovery-allowlist /usr/local/bin/recovery-rmimage
rm -f /etc/sudoers.d/ftd-grubcfg /etc/sudoers.d/ftd-rmimage /etc/sudoers.d/recovery-interface

log "removing dnsmasq + NFS config"
rm -f /etc/dnsmasq.d/clonezilla-pxe.conf
systemctl reload dnsmasq 2>/dev/null || true
# Strip the NFS export line; leave other exports untouched.
if grep -qE "^/srv/clonezilla-images[[:space:]]" /etc/exports; then
    sed -i.bak '\|^/srv/clonezilla-images[[:space:]]|d' /etc/exports
    exportfs -ra 2>/dev/null || true
fi

log "removing TFTP files"
rm -rf /srv/tftp/clonezilla /srv/tftp/debian-installer/amd64/grub/grub.cfg \
       /srv/tftp/ocs-backup.sh /srv/tftp/ocs-restore.sh /srv/tftp/grubnetx64.efi /srv/tftp/ipxe.0

log "removing app at $INSTALL_PREFIX"
rm -rf "$INSTALL_PREFIX"

if [[ $PURGE_STORAGE -eq 1 ]]; then
    log "unmounting /srv/clonezilla-images and removing bind fstab entry"
    umount /srv/clonezilla-images 2>/dev/null || true
    sed -i.bak '\|[[:space:]]/srv/clonezilla-images[[:space:]]|d' /etc/fstab
    rmdir /srv/clonezilla-images 2>/dev/null || true
    warn "underlying storage and /mnt/ftd-backup (if any) are NOT removed — "
    warn "remove their fstab entries manually if no longer needed."
fi

if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    if confirm "Delete service user '$SERVICE_USER'?" "n"; then
        userdel -r "$SERVICE_USER" 2>/dev/null || userdel "$SERVICE_USER"
    fi
fi

ok "uninstall complete"
[[ $PURGE_STORAGE -eq 0 ]] && echo "Backup data preserved at /srv/clonezilla-images (and its underlying mount)."
