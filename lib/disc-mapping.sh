#!/usr/bin/env bash
# Disc-mapping selector for the FTD Recovery installer.
# Sources lib/common.sh — caller must have already sourced it.
#
# Public entry point: choose_storage
# Side effects on success:
#   - /etc/fstab contains (mode 2/3) a UUID entry for the underlying device
#     and (all modes) a bind-mount line for /srv/clonezilla-images
#   - /srv/clonezilla-images is mounted and writable
# Exports: STORAGE_UNDERLYING (host path), STORAGE_BIND (always /srv/clonezilla-images)

STORAGE_BIND="/srv/clonezilla-images"
STORAGE_UNDERLYING=""

_show_block_devices() {
    echo
    echo "${DIM}Current block devices:${RESET}"
    lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,LABEL,MODEL
    echo
}

# _is_system_disk <disk-name> — true if this disk hosts / or /boot/firmware.
_is_system_disk() {
    local disk=$1 mp
    for mp in / /boot /boot/firmware; do
        local src
        src=$(findmnt -no SOURCE "$mp" 2>/dev/null || true)
        [[ -z "$src" ]] && continue
        local pkname
        pkname=$(lsblk -no PKNAME "$src" 2>/dev/null || true)
        [[ "$pkname" == "$disk" ]] && return 0
        # Also match if src itself is the disk (rare)
        [[ "$(basename "$src")" == "$disk" ]] && return 0
    done
    return 1
}

# _mode1_existing_mount — operator gives a path; we just bind-mount under it.
_mode1_existing_mount() {
    local path
    while true; do
        path=$(ask "Path to an existing directory that should hold backups" "/mnt/backup")
        if [[ -z "$path" ]]; then
            warn "path required"; continue
        fi
        case "$path" in
            /|/boot|/boot/*|/etc|/etc/*|/usr|/usr/*|/var|/var/log|/var/log/*|/proc/*|/sys/*|/dev/*)
                warn "refusing to use system path: $path"; continue ;;
        esac
        mkdir -p "$path" || { warn "could not create $path"; continue; }
        if ! touch "$path/.ftd-writetest" 2>/dev/null; then
            warn "$path is not writable by root"; continue
        fi
        rm -f "$path/.ftd-writetest"
        break
    done
    STORAGE_UNDERLYING="$path"
    ok "using existing path: $path"
}

# _mode2_adopt_partition [device] — pick a formatted partition, mount by UUID, set up bind.
# If device is given (from auto-detect) the selection step is skipped.
_mode2_adopt_partition() {
    local preselected=${1:-} dev

    if [[ -n "$preselected" ]]; then
        dev="$preselected"
    else
        local -a p_names=() p_sizes=() p_fstypes=() p_mounts=()
        local name pkname short fstype mp

        while read -r name; do
            [[ -z "$name" ]] && continue
            pkname=$(lsblk -no PKNAME "$name" 2>/dev/null | xargs || true)
            short=${pkname##*/}
            _is_system_disk "$short" && continue
            fstype=$(lsblk -no FSTYPE "$name" 2>/dev/null | xargs || true)
            [[ "$fstype" == "swap" ]] && continue
            mp=$(lsblk -no MOUNTPOINT "$name" 2>/dev/null | xargs || true)
            case "$mp" in /|/boot|/boot/*|/usr|/usr/*|/var|/var/*) continue ;; esac
            p_names+=("$name")
            p_sizes+=("$(lsblk -no SIZE "$name" | xargs)")
            p_fstypes+=("${fstype:-(none)}")
            p_mounts+=("${mp:-(not mounted)}")
        done < <(lsblk -lpno NAME | awk '$1 ~ /[0-9]$/')

        [[ ${#p_names[@]} -eq 0 ]] && die "no eligible partitions found (system disk excluded)"

        {
            echo
            echo "${BOLD}Available partitions:${RESET}"
            local i
            for i in "${!p_names[@]}"; do
                printf '  %s%d)%s %-15s  %-8s  %-10s  %s\n' \
                    "$BOLD" "$((i+1))" "$RESET" \
                    "${p_names[$i]}" "${p_sizes[$i]}" "${p_fstypes[$i]}" "${p_mounts[$i]}"
            done
            echo
        } >&2

        local reply
        while true; do
            read -r -p "${BOLD}Partition to use${RESET} [1-${#p_names[@]}]: " reply </dev/tty
            if [[ "$reply" =~ ^[0-9]+$ ]] && (( reply >= 1 && reply <= ${#p_names[@]} )); then
                dev="${p_names[$((reply-1))]}"; break
            fi
            warn "enter a number between 1 and ${#p_names[@]}"
        done
    fi

    local uuid fstype
    uuid=$(blkid -s UUID -o value "$dev" 2>/dev/null || true)
    fstype=$(blkid -s TYPE -o value "$dev" 2>/dev/null || true)
    [[ -z "$uuid" ]] && die "$dev has no filesystem — use option 3 to erase and format it first"
    [[ -z "$fstype" ]] && die "$dev has no recognized filesystem"
    log "found $dev: UUID=$uuid type=$fstype"

    # Keep existing mountpoint if already mounted, otherwise use default.
    local mountpoint
    local already_mounted
    already_mounted=$(findmnt -no TARGET --source "UUID=$uuid" 2>/dev/null || true)
    if [[ -n "$already_mounted" ]]; then
        mountpoint="$already_mounted"
        log "already mounted at $mountpoint"
    else
        mountpoint="/mnt/ftd-backup"
        mkdir -p "$mountpoint"
    fi

    if ! grep -qE "^UUID=$uuid[[:space:]]" /etc/fstab; then
        echo "UUID=$uuid  $mountpoint  $fstype  defaults,noatime,nofail  0  2" >> /etc/fstab
        ok "drive registered in fstab"
    else
        log "drive already registered in fstab"
    fi
    systemctl daemon-reload || true
    if ! findmnt -no TARGET "$mountpoint" >/dev/null 2>&1; then
        mount "$mountpoint" || die "mount $mountpoint failed"
    fi
    STORAGE_UNDERLYING="$mountpoint"
    ok "$dev mounted at $mountpoint"
}

# _disk_partition_info <disk> — compact summary of a disk's partitions for display.
# Shows labels and mountpoints so identical-model disks can be told apart.
_disk_partition_info() {
    local disk=$1
    local labels mounts
    labels=$(lsblk -lno LABEL "$disk" 2>/dev/null | tail -n +2 | grep -v '^[[:space:]]*$' | paste -sd',' -)
    mounts=$(lsblk -lno MOUNTPOINT "$disk" 2>/dev/null | tail -n +2 | grep -v '^[[:space:]]*$' | paste -sd',' -)
    if [[ -n "$labels" && -n "$mounts" ]]; then
        echo "$labels → $mounts"
    elif [[ -n "$mounts" ]]; then
        echo "mounted at $mounts"
    elif [[ -n "$labels" ]]; then
        echo "$labels (not mounted)"
    else
        echo "no partitions"
    fi
}

# _mode3_format_disk [device] — wipe + format a whole disk, mount, bind.
# If device is given (from auto-detect) the selection step is skipped.
_mode3_format_disk() {
    local preselected=${1:-} dev

    if [[ -n "$preselected" ]]; then
        dev="$preselected"
    else
        local -a d_names=() d_sizes=() d_models=() d_infos=()
        local name short risky

        while read -r name; do
            [[ -z "$name" ]] && continue
            short=${name##*/}
            [[ "$short" =~ ^(loop|zram|ram|sr) ]] && continue
            [[ "$(lsblk -dno TYPE "$name" 2>/dev/null)" != "disk" ]] && continue
            _is_system_disk "$short" && continue
            risky=""
            while read -r _child cmp; do
                case "$cmp" in /|/boot|/boot/*|/usr|/usr/*|/var|/var/*) risky=1 ;; esac
            done < <(lsblk -lpno NAME,MOUNTPOINT "$name" | tail -n +1 | grep -v "^$name ")
            [[ -n "$risky" ]] && continue
            d_names+=("$name")
            d_sizes+=("$(lsblk -dno SIZE "$name")")
            d_models+=("$(lsblk -dno MODEL "$name" | xargs || echo '?')")
            d_infos+=("$(_disk_partition_info "$name")")
        done < <(lsblk -dpno NAME)

        [[ ${#d_names[@]} -eq 0 ]] && die "no eligible drives found (system disk and drives in use are excluded)"

        {
            echo
            echo "${BOLD}Available drives:${RESET}"
            local i
            for i in "${!d_names[@]}"; do
                printf '  %s%d)%s %-15s  %-8s  %-28s  %s\n' \
                    "$BOLD" "$((i+1))" "$RESET" \
                    "${d_names[$i]}" "${d_sizes[$i]}" "${d_models[$i]}" "${d_infos[$i]}"
            done
            echo
        } >&2

        local reply
        while true; do
            read -r -p "${BOLD}Drive to erase and use${RESET} [1-${#d_names[@]}]: " reply </dev/tty
            if [[ "$reply" =~ ^[0-9]+$ ]] && (( reply >= 1 && reply <= ${#d_names[@]} )); then
                dev="${d_names[$((reply-1))]}"; break
            fi
            warn "enter a number between 1 and ${#d_names[@]}"
        done
    fi

    local size model serial
    size=$(lsblk -dno SIZE "$dev")
    model=$(lsblk -dno MODEL "$dev" | xargs)
    serial=$(lsblk -dno SERIAL "$dev" | xargs)

    echo
    echo "${RED}${BOLD}WARNING: this will permanently erase all data on:${RESET}"
    echo "  drive  : $model"
    echo "  size   : $size"
    echo "  device : $dev"
    echo
    [[ -z "$serial" ]] && die "cannot identify drive — refusing to erase without a stable identifier"
    if ! ask_typed_match "Type ERASE to confirm" "ERASE"; then
        die "confirmation failed — drive was not erased"
    fi

    log "partitioning and formatting $dev"
    parted -s "$dev" mklabel gpt
    parted -s -a optimal "$dev" mkpart primary ext4 0% 100%
    partprobe "$dev"
    udevadm settle

    local part
    part=$(lsblk -rnpo NAME "$dev" | awk -v d="$dev" '$1 != d {print; exit}')
    [[ -b "$part" ]] || die "could not locate new partition on $dev"
    log "formatting $part as ext4"
    mkfs.ext4 -F -L ftd-backup "$part"

    local uuid
    uuid=$(blkid -s UUID -o value "$part")
    [[ -n "$uuid" ]] || die "no UUID after formatting"

    local mountpoint="/mnt/ftd-backup"
    mkdir -p "$mountpoint"
    if ! grep -qE "^UUID=$uuid[[:space:]]" /etc/fstab; then
        echo "UUID=$uuid  $mountpoint  ext4  defaults,noatime,nofail  0  2" >> /etc/fstab
    fi
    systemctl daemon-reload || true
    mount "$mountpoint" || die "mount $mountpoint failed"
    STORAGE_UNDERLYING="$mountpoint"
    ok "drive formatted and mounted at $mountpoint"
}

# _setup_bind_mount — bind <STORAGE_UNDERLYING>/clonezilla-images → /srv/clonezilla-images
_setup_bind_mount() {
    local src="$STORAGE_UNDERLYING/clonezilla-images"
    mkdir -p "$src" "$STORAGE_BIND"

    local fstab_line="$src  $STORAGE_BIND  none  bind,nofail,x-systemd.requires-mounts-for=$STORAGE_UNDERLYING  0  0"
    if ! grep -qE "^[^#]*[[:space:]]$STORAGE_BIND[[:space:]]" /etc/fstab; then
        echo "$fstab_line" >> /etc/fstab
        ok "added bind-mount fstab entry"
    else
        log "bind-mount fstab entry already present"
    fi

    systemctl daemon-reload || true
    if ! findmnt -no TARGET "$STORAGE_BIND" >/dev/null 2>&1; then
        mount "$STORAGE_BIND" || die "bind-mount $STORAGE_BIND failed"
    fi

    touch "$STORAGE_BIND/.ftd-writetest" || die "$STORAGE_BIND is not writable"
    rm -f "$STORAGE_BIND/.ftd-writetest"
    ok "$STORAGE_BIND is mounted and writable"
}

choose_storage() {
    _show_block_devices

    # ── Auto-detect ─────────────────────────────────────────────────────────
    # Collect candidate partitions (mode 2 eligible).
    local -a cand_parts=()
    local name pkname short fstype mp risky

    while read -r name; do
        [[ -z "$name" ]] && continue
        pkname=$(lsblk -no PKNAME "$name" 2>/dev/null | xargs || true)
        short=${pkname##*/}
        _is_system_disk "$short" && continue
        fstype=$(lsblk -no FSTYPE "$name" 2>/dev/null | xargs || true)
        [[ "$fstype" == "swap" ]] && continue
        mp=$(lsblk -no MOUNTPOINT "$name" 2>/dev/null | xargs || true)
        case "$mp" in /|/boot|/boot/*|/usr|/usr/*|/var|/var/*) continue ;; esac
        cand_parts+=("$name")
    done < <(lsblk -lpno NAME | awk '$1 ~ /[0-9]$/')

    # Collect candidate disks (mode 3 eligible).
    local -a cand_disks=()
    while read -r name; do
        [[ -z "$name" ]] && continue
        short=${name##*/}
        [[ "$short" =~ ^(loop|zram|ram|sr) ]] && continue
        [[ "$(lsblk -dno TYPE "$name" 2>/dev/null)" != "disk" ]] && continue
        _is_system_disk "$short" && continue
        risky=""
        while read -r _c cmp; do
            case "$cmp" in /|/boot|/boot/*|/usr|/usr/*|/var|/var/*) risky=1 ;; esac
        done < <(lsblk -lpno NAME,MOUNTPOINT "$name" | grep -v "^$name ")
        [[ -n "$risky" ]] && continue
        cand_disks+=("$name")
    done < <(lsblk -dpno NAME)

    # Exactly one formatted partition → suggest adopting it.
    if [[ ${#cand_parts[@]} -eq 1 ]]; then
        local part="${cand_parts[0]}"
        local fstype_p mp_p size_p model_p pkname_p
        fstype_p=$(lsblk -no FSTYPE "$part" 2>/dev/null | xargs || true)
        if [[ -n "$fstype_p" ]]; then
            mp_p=$(lsblk -no MOUNTPOINT "$part" 2>/dev/null | xargs || true)
            size_p=$(lsblk -no SIZE "$part" | xargs)
            pkname_p=$(lsblk -no PKNAME "$part" 2>/dev/null | xargs || true)
            model_p=$(lsblk -no MODEL "$pkname_p" 2>/dev/null | xargs || echo "external drive")
            echo "${BOLD}Found one available drive:${RESET} ${model_p} — ${size_p}${mp_p:+ (already mounted at $mp_p)}"
            if confirm "Use it for backup storage?" "y"; then
                _mode2_adopt_partition "$part"
                _setup_bind_mount; return
            fi
        fi
    fi

    # No formatted partitions but exactly one blank disk → suggest erasing it.
    if [[ ${#cand_parts[@]} -eq 0 && ${#cand_disks[@]} -eq 1 ]]; then
        local disk="${cand_disks[0]}"
        local size_d model_d
        size_d=$(lsblk -dno SIZE "$disk")
        model_d=$(lsblk -dno MODEL "$disk" | xargs || echo "external drive")
        echo "${BOLD}Found one available drive:${RESET} ${model_d} — ${size_d} (not yet formatted)"
        warn "Setting it up will erase all data on the drive."
        if confirm "Set it up as backup storage?" "n"; then
            _mode3_format_disk "$disk"
            _setup_bind_mount; return
        fi
    fi

    # ── Manual selection ─────────────────────────────────────────────────────
    echo "${BOLD}How should backups be stored?${RESET}"
    echo "  1) Use a folder on this Pi (no extra drive needed)"
    echo "  2) Use an existing external drive (already formatted)"
    echo "  3) Erase and set up a blank drive (removes all data on that drive)"
    echo
    local choice
    while true; do
        choice=$(ask "Choose 1/2/3" "1")
        case "$choice" in
            1) _mode1_existing_mount; break ;;
            2) _mode2_adopt_partition; break ;;
            3) _mode3_format_disk; break ;;
            *) warn "enter 1, 2, or 3" ;;
        esac
    done
    _setup_bind_mount
}
