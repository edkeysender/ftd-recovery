#!/usr/bin/env bash
# FTD Recovery installer.
# Usage: sudo ./install.sh [--prefix PATH] [--user NAME] [--interface IF] [--server-ip IP]
#   or:  curl -fsSL https://github.com/edkeysender/ftd-recovery/raw/main/install.sh | sudo bash
#
# Default install prefix: /ftd/product/FTDRecovery
# Default service user:   ftd

set -euo pipefail

# When piped through `curl | bash`, BASH_SOURCE is empty and the lib/ files we
# need to source don't exist on disk — bootstrap by fetching the repo tarball
# and re-execing this installer from there.
if [[ -z "${BASH_SOURCE[0]:-}" || ! -f "${BASH_SOURCE[0]:-}" ]]; then
    REPO_URL="${FTD_RECOVERY_REPO_URL:-https://github.com/edkeysender/ftd-recovery}"
    REPO_REF="${FTD_RECOVERY_REPO_REF:-main}"
    BOOTSTRAP_DIR="$(mktemp -d -t ftd-recovery-XXXXXX)"
    echo "Fetching $REPO_URL @ $REPO_REF → $BOOTSTRAP_DIR"
    curl -fsSL "$REPO_URL/archive/refs/heads/$REPO_REF.tar.gz" \
        | tar -xz -C "$BOOTSTRAP_DIR" --strip-components=1
    exec bash "$BOOTSTRAP_DIR/install.sh" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/disc-mapping.sh
source "$SCRIPT_DIR/lib/disc-mapping.sh"

# ── Defaults ────────────────────────────────────────────────────────────────
INSTALL_PREFIX="/ftd/product/FTDRecovery"
SERVICE_USER="ftd"
INTERFACE=""
SERVER_IP=""
SUBNET_CIDR=""

# Clonezilla payload — override via env if mirrors change.
CLONEZILLA_VERSION="${CLONEZILLA_VERSION:-3.1.2-22}"
CLONEZILLA_ISO_URL="${CLONEZILLA_ISO_URL:-https://sourceforge.net/projects/clonezilla/files/clonezilla_live_stable/${CLONEZILLA_VERSION}/clonezilla-live-${CLONEZILLA_VERSION}-amd64.iso/download}"

# Debian netboot tarball (provides grubnetx64.efi + grub modules).
DEBIAN_NETBOOT_URL="${DEBIAN_NETBOOT_URL:-http://ftp.debian.org/debian/dists/stable/main/installer-amd64/current/images/netboot/netboot.tar.gz}"

# ── Args ────────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)     INSTALL_PREFIX="$2"; shift 2 ;;
        --user)       SERVICE_USER="$2"; shift 2 ;;
        --interface)  INTERFACE="$2"; shift 2 ;;
        --server-ip)  SERVER_IP="$2"; shift 2 ;;
        --subnet)     SUBNET_CIDR="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,6p' "$0"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done

require_root

echo
echo "${BOLD}${CYAN}FTD Recovery — installer${RESET}"
echo "${DIM}install prefix: $INSTALL_PREFIX${RESET}"
echo "${DIM}service user:   $SERVICE_USER${RESET}"
echo

# ── Step 1: dependencies ────────────────────────────────────────────────────
log "installing system packages"
export DEBIAN_FRONTEND=noninteractive
( apt-get update -qq && apt-get install -y -qq \
    python3 python3-venv python3-pip \
    dnsmasq tftpd-hpa arp-scan nfs-kernel-server \
    parted e2fsprogs \
    curl wget ca-certificates \
    isc-dhcp-common smartmontools \
    >/dev/null ) &
_spin $!
wait $!
# tftpd-hpa is installed for the tftp-hpa user/group; we still let dnsmasq serve TFTP.
systemctl disable --now tftpd-hpa 2>/dev/null || true
ok "system packages installed"

# ── Step 2: network detection ───────────────────────────────────────────────
if [[ -z "$INTERFACE" ]]; then
    INTERFACE=$(detect_default_iface || true)
fi
INTERFACE=$(ask_interface "Network interface for PXE/dnsmasq" "${INTERFACE:-eth0}")

# Warn when the chosen interface is also the default-route (internet-facing) NIC.
# Proxy-DHCP on a shared network can interfere with other DHCP servers.
DEFAULT_ROUTE_IFACE=$(detect_default_iface || true)
if [[ -n "$DEFAULT_ROUTE_IFACE" && "$DEFAULT_ROUTE_IFACE" == "$INTERFACE" ]]; then
    warn "$INTERFACE is also your internet connection."
    warn "This is fine on a small local network (a switch + recovery devices)."
    warn "On a large shared network it may disrupt other devices."
    if ! confirm "Is this Pi on a small local network? Continue?" "y"; then
        die "Aborted. Re-run and select a dedicated network interface."
    fi
fi

# Auto-detect IP and subnet from the chosen interface; CLI/env values win.
DETECTED_IP=$(detect_iface_ip "$INTERFACE" || true)
DETECTED_CIDR=$(detect_iface_cidr "$INTERFACE" 2>/dev/null || true)
SERVER_IP="${SERVER_IP:-$DETECTED_IP}"
SUBNET_CIDR="${SUBNET_CIDR:-$DETECTED_CIDR}"

if [[ -n "$SERVER_IP" && -n "$SUBNET_CIDR" ]]; then
    echo "${DIM}Detected:  server-ip=${SERVER_IP}  subnet=${SUBNET_CIDR}${RESET}"
    if ! confirm "Use detected network config?" "y"; then
        SERVER_IP=$(ask "This Pi's static IP on $INTERFACE (clients fetch boot files from here)" "$SERVER_IP")
        SUBNET_CIDR=$(ask "Subnet CIDR served by proxy-DHCP" "$SUBNET_CIDR")
    fi
else
    SERVER_IP=$(ask "This Pi's static IP on $INTERFACE (clients fetch boot files from here)" "$SERVER_IP")
    SUBNET_CIDR=$(ask "Subnet CIDR served by proxy-DHCP" "$SUBNET_CIDR")
fi

[[ -z "$SERVER_IP" ]] && die "server IP is required"
[[ -z "$SUBNET_CIDR" ]] && die "subnet CIDR is required"
SUBNET_BASE=$(subnet_base "$SUBNET_CIDR")

ok "interface=$INTERFACE  server-ip=$SERVER_IP  subnet=$SUBNET_CIDR"

# ── Step 3: storage (disc mapping) ──────────────────────────────────────────
choose_storage

# ── Step 4: service user ────────────────────────────────────────────────────
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "creating service user: $SERVICE_USER"
    useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "user $SERVICE_USER created"
else
    log "user $SERVICE_USER already exists"
fi

# ── Step 5: app layout ──────────────────────────────────────────────────────
log "installing application to $INSTALL_PREFIX"
mkdir -p "$INSTALL_PREFIX"
# Hand the prefix to the service user up-front so `sudo -u $SERVICE_USER python3 -m venv …`
# (and pip install) can write inside it.
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PREFIX"
install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SCRIPT_DIR/app/app.py"               "$INSTALL_PREFIX/app.py"
install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SCRIPT_DIR/VERSION"                   "$INSTALL_PREFIX/VERSION"
install -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SCRIPT_DIR/app/dhcp_namesniffer.py"  "$INSTALL_PREFIX/dhcp_namesniffer.py"
install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SCRIPT_DIR/app/requirements.txt"     "$INSTALL_PREFIX/requirements.txt"
if [[ ! -e "$INSTALL_PREFIX/hosts.yml" ]]; then
    install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SCRIPT_DIR/app/hosts.yml.example" "$INSTALL_PREFIX/hosts.yml"
    log "created empty hosts.yml"
else
    log "keeping existing hosts.yml"
fi

if [[ ! -d "$INSTALL_PREFIX/venv" ]]; then
    log "creating Python venv"
    sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_PREFIX/venv"
fi
log "installing Python dependencies"
sudo -u "$SERVICE_USER" "$INSTALL_PREFIX/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_PREFIX/venv/bin/pip" install --quiet -r "$INSTALL_PREFIX/requirements.txt"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PREFIX"
ok "app installed"

# ── Step 6: TFTP layout ─────────────────────────────────────────────────────
log "laying down TFTP tree under /srv/tftp"
mkdir -p /srv/tftp/debian-installer/amd64/grub /srv/tftp/clonezilla

render "$SCRIPT_DIR/tftp/ocs-backup.sh"  /srv/tftp/ocs-backup.sh  0755 "SERVER_IP=$SERVER_IP"
render "$SCRIPT_DIR/tftp/ocs-restore.sh" /srv/tftp/ocs-restore.sh 0755 "SERVER_IP=$SERVER_IP"
install -m 0644 "$SCRIPT_DIR/tftp/debian-installer/amd64/grub/grub.cfg" \
                /srv/tftp/debian-installer/amd64/grub/grub.cfg

# Debian netboot tarball → provides grubnetx64.efi + grub modules.
if [[ ! -s /srv/tftp/grubnetx64.efi ]]; then
    log "downloading Debian netboot tarball for grubnetx64.efi"
    tmp=$(mktemp -d)
    curl -fL --progress-bar "$DEBIAN_NETBOOT_URL" -o "$tmp/netboot.tar.gz"
    tar -xzf "$tmp/netboot.tar.gz" -C "$tmp"
    # Locate grub EFI — filename and path vary across Debian releases.
    grub_efi=$(find "$tmp" \( -name "grubx64.efi" -o -name "grubnetx64.efi" -o -name "grubnetx64.efi.signed" \) \
               ! -name "*.sig" | head -1)
    [[ -z "$grub_efi" ]] && die "could not find grub EFI in Debian netboot tarball — check DEBIAN_NETBOOT_URL"
    grub_dir=$(dirname "$grub_efi")
    install -m 0644 "$grub_efi" /srv/tftp/grubnetx64.efi
    [[ -d "$grub_dir/x86_64-efi" ]] && cp -r "$grub_dir/x86_64-efi" /srv/tftp/debian-installer/amd64/grub/
    [[ -f "$grub_dir/unicode.pf2" ]] && install -m 0644 "$grub_dir/unicode.pf2" /srv/tftp/debian-installer/amd64/grub/unicode.pf2
    rm -rf "$tmp"
fi
# NICs configured for iPXE filename will request ipxe.0 — point it at GRUB.
ln -sfn /srv/tftp/grubnetx64.efi /srv/tftp/ipxe.0
ok "TFTP boot files in place"

# Clonezilla payload — vmlinuz, initrd.img, filesystem.squashfs.
if [[ ! -s /srv/tftp/clonezilla/vmlinuz \
   || ! -s /srv/tftp/clonezilla/initrd.img \
   || ! -s /srv/tftp/clonezilla/filesystem.squashfs ]]; then
    log "downloading Clonezilla live ${CLONEZILLA_VERSION} (~700 MB)"
    tmp=$(mktemp -d)
    curl -fL --progress-bar "$CLONEZILLA_ISO_URL" -o "$tmp/cz.iso"
    mkdir -p "$tmp/iso"
    mount -o loop,ro "$tmp/cz.iso" "$tmp/iso"
    install -m 0644 "$tmp/iso/live/vmlinuz"            /srv/tftp/clonezilla/vmlinuz
    install -m 0644 "$tmp/iso/live/initrd.img"         /srv/tftp/clonezilla/initrd.img
    install -m 0644 "$tmp/iso/live/filesystem.squashfs" /srv/tftp/clonezilla/filesystem.squashfs
    umount "$tmp/iso"
    rm -rf "$tmp"
    ok "Clonezilla payload extracted"
else
    log "Clonezilla payload already present"
fi

# ── Step 7: helper scripts + sudoers ────────────────────────────────────────
log "installing helper scripts and sudoers fragments"
mkdir -p /usr/local/lib/ftd-recovery
install -m 0644 "$SCRIPT_DIR/lib/common.sh"       /usr/local/lib/ftd-recovery/common.sh
install -m 0644 "$SCRIPT_DIR/lib/disc-mapping.sh" /usr/local/lib/ftd-recovery/disc-mapping.sh
render "$SCRIPT_DIR/helpers/recovery-grubcfg" /usr/local/bin/recovery-grubcfg 0755 "SERVER_IP=$SERVER_IP"
install -m 0755 "$SCRIPT_DIR/helpers/recovery-allowlist"     /usr/local/bin/recovery-allowlist
install -m 0755 "$SCRIPT_DIR/helpers/recovery-rmimage"       /usr/local/bin/recovery-rmimage
install -m 0755 "$SCRIPT_DIR/helpers/recovery-remount"       /usr/local/bin/recovery-remount
install -m 0755 "$SCRIPT_DIR/helpers/recovery-change-storage" /usr/local/bin/recovery-change-storage

for f in ftd-grubcfg ftd-rmimage recovery-interface; do
    # Rewrite the leading user token to whatever SERVICE_USER is.
    sed "s|^ftd ALL=|$SERVICE_USER ALL=|" "$SCRIPT_DIR/sudoers.d/$f" \
        > "/etc/sudoers.d/$f.tmp"
    chmod 0440 "/etc/sudoers.d/$f.tmp"
    if visudo -cf "/etc/sudoers.d/$f.tmp" >/dev/null; then
        mv "/etc/sudoers.d/$f.tmp" "/etc/sudoers.d/$f"
    else
        rm -f "/etc/sudoers.d/$f.tmp"
        die "sudoers fragment $f failed visudo -c check"
    fi
done
ok "helpers + sudoers installed"

# ── Step 8: dnsmasq ─────────────────────────────────────────────────────────
log "configuring dnsmasq (proxy-DHCP + TFTP)"
render "$SCRIPT_DIR/dnsmasq.d/clonezilla-pxe.conf" /etc/dnsmasq.d/clonezilla-pxe.conf 0644 \
    "INTERFACE=$INTERFACE" "SUBNET_BASE=$SUBNET_BASE"
mkdir -p /etc/clonezilla-server
touch /etc/clonezilla-server/dhcp-hosts.conf
chmod 0644 /etc/clonezilla-server/dhcp-hosts.conf
systemctl enable --now dnsmasq >/dev/null
systemctl reload dnsmasq
ok "dnsmasq up"

# ── Step 9: NFS export ──────────────────────────────────────────────────────
log "exporting /srv/clonezilla-images over NFS"
EXPORT_LINE=$(sed "s|__SUBNET_CIDR__|$SUBNET_CIDR|g" "$SCRIPT_DIR/nfs/exports.append")
if ! grep -qE "^/srv/clonezilla-images[[:space:]]" /etc/exports; then
    echo "$EXPORT_LINE" >> /etc/exports
fi
systemctl enable --now nfs-server >/dev/null
exportfs -ra
ok "NFS export active"

# ── Step 10: systemd units ──────────────────────────────────────────────────
log "installing systemd units"
render "$SCRIPT_DIR/systemd/recovery-interface.service" /etc/systemd/system/recovery-interface.service 0644 \
    "INSTALL_PREFIX=$INSTALL_PREFIX" "SERVICE_USER=$SERVICE_USER" \
    "INTERFACE=$INTERFACE" "SERVER_IP=$SERVER_IP"
render "$SCRIPT_DIR/systemd/recovery-dhcp-sniffer.service" /etc/systemd/system/recovery-dhcp-sniffer.service 0644 \
    "INSTALL_PREFIX=$INSTALL_PREFIX" "INTERFACE=$INTERFACE"
install -m 0644 "$SCRIPT_DIR/systemd/clonezilla-http.service" /etc/systemd/system/clonezilla-http.service

systemctl daemon-reload
systemctl enable --now recovery-interface clonezilla-http recovery-dhcp-sniffer >/dev/null
ok "services enabled and started"

# ── Step 11: summary ────────────────────────────────────────────────────────
echo
echo "${BOLD}${GREEN}Installation complete!${RESET}"
echo
echo "  Open this address in your browser to get started:"
echo
echo "  ${BOLD}${CYAN}http://$SERVER_IP:8088/${RESET}"
echo

# Static IP guidance — detect which network manager is in use.
IP_NOTE=""
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    IP_NOTE="To set a static IP: run ${BOLD}nmtui${RESET}, choose Edit Connection → ${INTERFACE} → IPv4 → Manual."
elif [[ -f /etc/dhcpcd.conf ]]; then
    IP_NOTE="To set a static IP, add these lines to ${BOLD}/etc/dhcpcd.conf${RESET} and reboot:
  interface $INTERFACE
  static ip_address=$SERVER_IP/$(echo "$SUBNET_CIDR" | cut -d/ -f2)
  static routers=<your-router-ip>"
else
    IP_NOTE="To set a static IP, configure ${BOLD}$INTERFACE${RESET} in your network manager or /etc/network/interfaces and reboot."
fi

echo "${YELLOW}Note:${RESET} the IP address above must not change after installation."
echo "$IP_NOTE"
echo
echo "${DIM}Troubleshooting:"
echo "  systemctl status recovery-interface clonezilla-http recovery-dhcp-sniffer dnsmasq nfs-server"
echo "  curl http://$SERVER_IP:8088/api/status${RESET}"
