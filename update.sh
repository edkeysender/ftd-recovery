#!/usr/bin/env bash
# FTD Recovery updater.
# Updates app, lib scripts, helpers, and sudoers from the repo without
# touching any user configuration or backup data.
#
# Usage:
#   curl -fsSL https://github.com/edkeysender/ftd-recovery/raw/main/update.sh | sudo bash
#   sudo ./update.sh [--ref <branch-or-tag>]

set -euo pipefail

# ── Bootstrap (curl | bash re-exec) ─────────────────────────────────────────
if [[ -z "${BASH_SOURCE[0]:-}" || ! -f "${BASH_SOURCE[0]:-}" ]]; then
    REPO_URL="${FTD_RECOVERY_REPO_URL:-https://github.com/edkeysender/ftd-recovery}"
    REPO_REF="${FTD_RECOVERY_REPO_REF:-main}"
    BOOTSTRAP_DIR="$(mktemp -d -t ftd-recovery-update-XXXXXX)"
    echo "Fetching $REPO_URL @ $REPO_REF → $BOOTSTRAP_DIR"
    curl -fsSL "$REPO_URL/archive/refs/heads/$REPO_REF.tar.gz" \
        | tar -xz -C "$BOOTSTRAP_DIR" --strip-components=1
    exec bash "$BOOTSTRAP_DIR/update.sh" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

# ── Args ────────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref) REPO_REF="$2"; shift 2 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done

require_root

echo
echo "${BOLD}${CYAN}FTD Recovery — update${RESET}"
echo

# ── Detect install prefix from live service ──────────────────────────────────
INSTALL_PREFIX=$(systemctl show recovery-interface --property=WorkingDirectory --value 2>/dev/null || true)
if [[ -z "$INSTALL_PREFIX" || ! -d "$INSTALL_PREFIX" ]]; then
    INSTALL_PREFIX="/ftd/product/FTDRecovery"
fi
if [[ ! -d "$INSTALL_PREFIX" ]]; then
    die "install prefix not found ($INSTALL_PREFIX) — is FTD Recovery installed?"
fi
echo "${DIM}install prefix: $INSTALL_PREFIX${RESET}"
echo

# ── Step 1: system packages (install missing only) ───────────────────────────
log "checking system packages"
export DEBIAN_FRONTEND=noninteractive
missing=()
for pkg in smartmontools; do
    dpkg -s "$pkg" &>/dev/null || missing+=("$pkg")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    ( apt-get update -qq && apt-get install -y -qq "${missing[@]}" >/dev/null ) & _spin $!; wait $!
    ok "installed: ${missing[*]}"
else
    ok "packages up to date"
fi

# ── Step 2: lib files ────────────────────────────────────────────────────────
log "updating lib files"
mkdir -p /usr/local/lib/ftd-recovery
install -m 0644 "$SCRIPT_DIR/lib/common.sh"       /usr/local/lib/ftd-recovery/common.sh
install -m 0644 "$SCRIPT_DIR/lib/disc-mapping.sh" /usr/local/lib/ftd-recovery/disc-mapping.sh
ok "lib files updated"

# ── Step 3: helper scripts ───────────────────────────────────────────────────
log "updating helper scripts"
install -m 0755 "$SCRIPT_DIR/helpers/recovery-remount"        /usr/local/bin/recovery-remount
install -m 0755 "$SCRIPT_DIR/helpers/recovery-change-storage" /usr/local/bin/recovery-change-storage
install -m 0755 "$SCRIPT_DIR/helpers/recovery-allowlist"      /usr/local/bin/recovery-allowlist
ok "helpers updated"

# ── Step 3a: dnsmasq config ──────────────────────────────────────────────────
log "updating dnsmasq config"
IFACE=$(grep -oP '(?<=^interface=)\S+' /etc/dnsmasq.d/clonezilla-pxe.conf 2>/dev/null || true)
SUBNET=$(grep -oP '(?<=^dhcp-range=)\S+(?=,proxy)' /etc/dnsmasq.d/clonezilla-pxe.conf 2>/dev/null || true)
if [[ -n "$IFACE" && -n "$SUBNET" ]]; then
    sed -e "s|__INTERFACE__|$IFACE|g" -e "s|__SUBNET_BASE__|$SUBNET|g" \
        "$SCRIPT_DIR/dnsmasq.d/clonezilla-pxe.conf" > /etc/dnsmasq.d/clonezilla-pxe.conf
    if systemctl is-active --quiet dnsmasq; then
        systemctl reload dnsmasq
    else
        systemctl start dnsmasq || warn "dnsmasq failed to start — check: journalctl -u dnsmasq -n 20"
    fi
    ok "dnsmasq config updated"
else
    warn "could not detect interface/subnet from existing dnsmasq config — skipping"
fi

# ── Step 4: sudoers ──────────────────────────────────────────────────────────
log "updating sudoers"
install -m 0440 "$SCRIPT_DIR/sudoers.d/recovery-interface" /etc/sudoers.d/recovery-interface
ok "sudoers updated"

# ── Step 5: app ──────────────────────────────────────────────────────────────
log "updating app"
install -m 0644 "$SCRIPT_DIR/app/app.py" "$INSTALL_PREFIX/app.py"
install -m 0644 "$SCRIPT_DIR/VERSION"    "$INSTALL_PREFIX/VERSION"
ok "app updated"

# ── Step 6: OCS scripts (ocs-backup.sh / ocs-restore.sh) ────────────────────
log "updating OCS scripts"
SERVER_IP=$(sed -nE 's|.*API="http://([^:]+):.*|\1|p' /srv/tftp/ocs-backup.sh 2>/dev/null | head -1 || true)
if [[ -n "$SERVER_IP" ]]; then
    sed "s|__SERVER_IP__|$SERVER_IP|g" "$SCRIPT_DIR/tftp/ocs-backup.sh"  > /srv/tftp/ocs-backup.sh
    sed "s|__SERVER_IP__|$SERVER_IP|g" "$SCRIPT_DIR/tftp/ocs-restore.sh" > /srv/tftp/ocs-restore.sh
    chmod 0755 /srv/tftp/ocs-backup.sh /srv/tftp/ocs-restore.sh
    ok "OCS scripts updated (server IP: $SERVER_IP)"
else
    warn "could not detect server IP from existing OCS scripts — skipping"
fi

# ── Step 7: Clonezilla payload ───────────────────────────────────────────────
CLONEZILLA_VERSION="${CLONEZILLA_VERSION:-3.3.3-15}"
CZ_VERSION_FILE="/srv/tftp/clonezilla/VERSION"
current_cz=$(cat "$CZ_VERSION_FILE" 2>/dev/null || echo "")
if [[ "$current_cz" == "$CLONEZILLA_VERSION" ]]; then
    ok "Clonezilla $CLONEZILLA_VERSION already current"
else
    log "updating Clonezilla $current_cz → $CLONEZILLA_VERSION (~700 MB, please wait)"
    CZ_URL="https://sourceforge.net/projects/clonezilla/files/clonezilla_live_stable/${CLONEZILLA_VERSION}/clonezilla-live-${CLONEZILLA_VERSION}-amd64.iso/download"
    tmp=$(mktemp -d -t clonezilla-XXXXXX)
    curl -fL --progress-bar "$CZ_URL" -o "$tmp/cz.iso"
    mkdir -p "$tmp/iso"
    mount -o loop,ro "$tmp/cz.iso" "$tmp/iso"
    install -m 0644 "$tmp/iso/live/vmlinuz"             /srv/tftp/clonezilla/vmlinuz
    install -m 0644 "$tmp/iso/live/initrd.img"          /srv/tftp/clonezilla/initrd.img
    install -m 0644 "$tmp/iso/live/filesystem.squashfs" /srv/tftp/clonezilla/filesystem.squashfs
    umount "$tmp/iso"
    echo "$CLONEZILLA_VERSION" > "$CZ_VERSION_FILE"
    rm -rf "$tmp"
    ok "Clonezilla updated to $CLONEZILLA_VERSION"
fi

# ── Step 8: restart services ─────────────────────────────────────────────────
log "restarting services"
systemctl restart recovery-interface
ok "recovery-interface restarted"

echo
echo "${BOLD}${GREEN}Update complete.${RESET}"
