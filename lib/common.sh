#!/usr/bin/env bash
# Shared helpers for the FTD Recovery installer.
# Sourced by install.sh — not meant to be executed directly.

set -euo pipefail

if [[ -t 1 ]]; then
    BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GREEN=$'\e[32m'
    YELLOW=$'\e[33m'; BLUE=$'\e[34m'; CYAN=$'\e[36m'; RESET=$'\e[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; CYAN=""; RESET=""
fi

log()   { printf '%s==>%s %s\n'    "$BLUE"   "$RESET" "$*"; }
ok()    { printf '%s ok%s %s\n'    "$GREEN"  "$RESET" "$*"; }
warn()  { printf '%swarn%s %s\n'   "$YELLOW" "$RESET" "$*" >&2; }
err()   { printf '%serror%s %s\n'  "$RED"    "$RESET" "$*" >&2; }
die()   { err "$*"; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || die "this script must run as root (try: sudo $0)"
}

# ask "Question" "default" → echoes the answer (default on empty input)
ask() {
    local prompt=$1 default=${2:-} reply
    if [[ -n "$default" ]]; then
        read -r -p "${BOLD}${prompt}${RESET} [${default}]: " reply </dev/tty
        echo "${reply:-$default}"
    else
        read -r -p "${BOLD}${prompt}${RESET}: " reply </dev/tty
        echo "$reply"
    fi
}

# confirm "Question" "y|n" → 0 if yes, 1 if no
confirm() {
    local prompt=$1 default=${2:-n} reply
    local hint='[y/N]'; [[ "$default" == "y" ]] && hint='[Y/n]'
    read -r -p "${BOLD}${prompt}${RESET} ${hint}: " reply </dev/tty
    reply=${reply:-$default}
    [[ "$reply" =~ ^[Yy]$ ]]
}

# ask_typed "type the value to confirm" "expected" → 0 if matches
ask_typed_match() {
    local prompt=$1 expected=$2 reply
    read -r -p "${BOLD}${prompt}${RESET}: " reply </dev/tty
    [[ "$reply" == "$expected" ]]
}

# detect_default_iface — prints the interface used by the default route.
detect_default_iface() {
    ip route show default 2>/dev/null | awk '/default/ {print $5; exit}'
}

# detect_iface_ip <iface> — prints the first IPv4 address on iface.
detect_iface_ip() {
    ip -4 -o addr show dev "$1" 2>/dev/null \
        | awk '{print $4}' | cut -d/ -f1 | head -1
}

# detect_iface_cidr <iface> — prints the network CIDR for iface (e.g. 192.168.1.0/24).
detect_iface_cidr() {
    local cidr
    cidr=$(ip -4 -o addr show dev "$1" 2>/dev/null | awk '{print $4}' | head -1)
    [[ -z "$cidr" ]] && return 1
    # Use ipcalc-like derivation via python (always available on Debian).
    python3 -c "import ipaddress,sys; print(ipaddress.ip_interface('$cidr').network)" 2>/dev/null
}

# subnet_base <cidr> — prints the network address without /prefix (e.g. 192.168.1.0).
subnet_base() {
    echo "${1%/*}"
}

# template_file <src> <dst> [var=value]... — substitute __VAR__ placeholders.
template_file() {
    local src=$1 dst=$2; shift 2
    local content
    content=$(cat "$src")
    local pair var val
    for pair in "$@"; do
        var=${pair%%=*}; val=${pair#*=}
        # Use a literal-safe replacement: route val through a sed that handles
        # delimiter chars by using ascii control char as the separator.
        content=$(printf '%s' "$content" | sed "s${RS}__${var}__${RS}${val//\\/\\\\}${RS}g")
    done
    install -m 0644 /dev/null "$dst"
    printf '%s' "$content" > "$dst"
}

# RS = ASCII record-separator, safe sed delimiter (path values contain /).
RS=$'\036'

# render <src> <dst> <mode> <var=value>... — sed-based templater that preserves
# file mode and handles paths-with-slashes by using RS as the sed delimiter.
render() {
    local src=$1 dst=$2 mode=$3; shift 3
    local args=()
    local pair var val
    for pair in "$@"; do
        var=${pair%%=*}; val=${pair#*=}
        args+=(-e "s${RS}__${var}__${RS}${val}${RS}g")
    done
    sed "${args[@]}" "$src" > "$dst"
    chmod "$mode" "$dst"
}
