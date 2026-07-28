#!/usr/bin/env bash
# FTD Recovery updater.
# Updates app, lib scripts, helpers, and sudoers from the repo without
# touching any user configuration or backup data.
#
# Usage:
#   curl -fsSL http://gitlab.ftdinternal.aero/ftd-supp/ftd_recovery/-/raw/main/update.sh | sudo bash
#   sudo ./update.sh [--ref <branch-or-tag>]

set -euo pipefail

# ── Bootstrap (curl | bash re-exec) ─────────────────────────────────────────
if [[ -z "${BASH_SOURCE[0]:-}" || ! -f "${BASH_SOURCE[0]:-}" ]]; then
    REPO_URL="${FTD_RECOVERY_REPO_URL:-http://gitlab.ftdinternal.aero/ftd-supp/ftd_recovery}"
    REPO_REF="${FTD_RECOVERY_REPO_REF:-main}"
    if [[ "$REPO_URL" == *github.com* ]]; then
        ARCHIVE_URL="$REPO_URL/archive/refs/heads/$REPO_REF.tar.gz"
    else
        # GitLab archive URL format
        ARCHIVE_URL="$REPO_URL/-/archive/$REPO_REF/ftd-recovery-$REPO_REF.tar.gz"
    fi
    BOOTSTRAP_DIR="$(mktemp -d -t ftd-recovery-update-XXXXXX)"
    echo "Fetching $REPO_URL @ $REPO_REF → $BOOTSTRAP_DIR"
    curl -fsSL "$ARCHIVE_URL" \
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
for pkg in smartmontools git strongswan strongswan-swanctl xl2tpd ppp; do
    dpkg -s "$pkg" &>/dev/null || missing+=("$pkg")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    ( apt-get update -qq && apt-get install -y -qq "${missing[@]}" >/dev/null ) & _spin $!
    if wait $!; then
        ok "installed: ${missing[*]}"
        # Debian auto-starts these on install; the update helper brings them
        # up on demand instead. Only touched when freshly installed, so a
        # VPN-carried update can never stop its own tunnel.
        case " ${missing[*]} " in
            *" strongswan "*|*" strongswan-swanctl "*|*" xl2tpd "*)
                systemctl disable --now xl2tpd strongswan-starter 2>/dev/null || true ;;
        esac
    else
        warn "package install failed (no internet access?) — still missing: ${missing[*]}"
    fi
else
    ok "packages up to date"
fi

# ── Step 1a: VPN daemon (strongSwan 6) ───────────────────────────────────────
# strongSwan 6 removed the legacy `ipsec`/starter interface; the VPN helper
# drives charon-systemd over vici with swanctl. Retire the conflicting legacy
# starter, mask the unusable swanctl unit some builds ship in a 'bad' state,
# and smoke-test that charon is actually reachable — so a device never looks
# "updated" while silently unable to bring up the field VPN.
if command -v swanctl &>/dev/null; then
    log "verifying VPN stack (strongSwan 6)"
    systemctl disable --now strongswan-starter.service 2>/dev/null || true
    systemctl mask strongswan-swanctl.service 2>/dev/null || true
    vpn_ok=0
    # charon needs a moment to open its vici socket after start — retry.
    _swan_ready() { local i; for i in $(seq 1 12); do swanctl --stats &>/dev/null && return 0; sleep 1; done; return 1; }
    if systemctl is-active --quiet strongswan.service || systemctl is-active --quiet ipsec.service; then
        # Daemon already up (e.g. this very update is running over the VPN) —
        # don't disturb it, just probe.
        _swan_ready && vpn_ok=1
    else
        for svc in strongswan.service ipsec.service; do
            systemctl cat "$svc" &>/dev/null || continue
            systemctl start "$svc" 2>/dev/null || true
            _swan_ready && vpn_ok=1
            systemctl stop "$svc" 2>/dev/null || true
            break
        done
    fi
    if [[ $vpn_ok -eq 1 ]]; then
        ok "VPN stack functional (charon reachable via swanctl)"
    else
        warn "VPN stack NOT functional — field (off-network) updates will fail."
        warn "  debug: sudo systemctl start strongswan.service && sudo swanctl --stats"
    fi
else
    warn "swanctl not installed — field (off-network) updates will not work."
    warn "  install in-network: sudo apt-get install -y strongswan strongswan-swanctl xl2tpd ppp"
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
install -m 0755 "$SCRIPT_DIR/helpers/recovery-update"         /usr/local/bin/recovery-update
ok "helpers updated"

# ── Step 3b: self-update config ──────────────────────────────────────────────
log "updating self-update config"
mkdir -p /etc/ftd-recovery
install -m 0600 "$SCRIPT_DIR/etc/update.conf.example" /etc/ftd-recovery/update.conf.example
if [[ ! -f /etc/ftd-recovery/update.conf ]]; then
    install -m 0600 "$SCRIPT_DIR/etc/update.conf.example" /etc/ftd-recovery/update.conf
    ok "seeded /etc/ftd-recovery/update.conf (edit GITLAB_IP + VPN config for field devices)"
else
    ok "kept existing /etc/ftd-recovery/update.conf"
fi

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
SERVICE_USER=$(systemctl show recovery-interface --property=User --value 2>/dev/null || true)
[[ -z "$SERVICE_USER" ]] && SERVICE_USER=ftd
sed "s|^ftd ALL=|$SERVICE_USER ALL=|" "$SCRIPT_DIR/sudoers.d/recovery-interface" \
    > /etc/sudoers.d/recovery-interface.tmp
chmod 0440 /etc/sudoers.d/recovery-interface.tmp
if visudo -cf /etc/sudoers.d/recovery-interface.tmp >/dev/null; then
    mv /etc/sudoers.d/recovery-interface.tmp /etc/sudoers.d/recovery-interface
else
    rm -f /etc/sudoers.d/recovery-interface.tmp
    die "sudoers fragment recovery-interface failed visudo -c check"
fi
ok "sudoers updated (user: $SERVICE_USER)"

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
    # SourceForge is unreachable from field devices (updates arrive over a
    # split VPN tunnel to the internal network only) — skip, don't fail.
    if curl -fL --progress-bar "$CZ_URL" -o "$tmp/cz.iso"; then
        mkdir -p "$tmp/iso"
        mount -o loop,ro "$tmp/cz.iso" "$tmp/iso"
        install -m 0644 "$tmp/iso/live/vmlinuz"             /srv/tftp/clonezilla/vmlinuz
        install -m 0644 "$tmp/iso/live/initrd.img"          /srv/tftp/clonezilla/initrd.img
        install -m 0644 "$tmp/iso/live/filesystem.squashfs" /srv/tftp/clonezilla/filesystem.squashfs
        umount "$tmp/iso"
        echo "$CLONEZILLA_VERSION" > "$CZ_VERSION_FILE"
        ok "Clonezilla updated to $CLONEZILLA_VERSION"
    else
        warn "Clonezilla download failed (no internet access?) — keeping $current_cz"
    fi
    rm -rf "$tmp"
fi

# ── Step 8: nfs-server autostart + ordering ──────────────────────────────────
log "configuring nfs-server autostart"
mkdir -p /etc/systemd/system/nfs-server.service.d
install -m 0644 "$SCRIPT_DIR/systemd/nfs-server.service.d/ftd-recovery.conf" \
    /etc/systemd/system/nfs-server.service.d/ftd-recovery.conf
systemctl enable nfs-server 2>/dev/null || true
systemctl daemon-reload
ok "nfs-server enabled and ordered after bind mount"

# ── Step 9: restart services ─────────────────────────────────────────────────
log "restarting services"
if mountpoint -q /srv/clonezilla-images 2>/dev/null; then
    systemctl restart nfs-server 2>/dev/null || true
    exportfs -ra 2>/dev/null || true
else
    warn "/srv/clonezilla-images not mounted — nfs-server not started (run: sudo mount -a)"
fi
systemctl restart recovery-interface
ok "services restarted"

echo
echo "${BOLD}${GREEN}Update complete.${RESET}"
