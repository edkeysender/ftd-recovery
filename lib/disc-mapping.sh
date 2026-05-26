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
    ok "using existing mount: $path"
}

# _list_candidate_partitions — prints partitions that are NOT on the system disk
# and NOT currently mounted to a system directory.
_list_candidate_partitions() {
    local line name size fstype mountpoint pkname
    while IFS=$'\t' read -r name size fstype mountpoint pkname; do
        [[ "$fstype" == "swap" ]] && continue
        # skip system disk's children
        if _is_system_disk "$pkname"; then continue; fi
        # skip already mounted to /, /boot*, /usr, /var, /srv/clonezilla-images
        case "$mountpoint" in
            /|/boot|/boot/*|/usr|/usr/*|/var|/var/*) continue ;;
        esac
        printf '  %s\t%s\t%s\t%s\n' "$name" "$size" "${fstype:-(none)}" "${mountpoint:-(unmounted)}"
    done < <(lsblk -rnpo NAME,SIZE,FSTYPE,MOUNTPOINT,PKNAME | awk '$1 ~ /[0-9]$/')
}

# _mode2_adopt_partition — pick an existing partition, mount by UUID, set up bind.
_mode2_adopt_partition() {
    echo
    echo "${BOLD}Eligible partitions (system disk excluded):${RESET}"
    printf '  %s\t%s\t%s\t%s\n' DEVICE SIZE FSTYPE MOUNTPOINT
    _list_candidate_partitions
    echo
    local dev
    while true; do
        dev=$(ask "Partition device (e.g. /dev/nvme1n1p1)" "")
        [[ -b "$dev" ]] || { warn "$dev is not a block device"; continue; }
        if _is_system_disk "$(lsblk -no PKNAME "$dev")"; then
            warn "refusing: $dev belongs to the system disk"; continue
        fi
        break
    done
    local uuid fstype
    uuid=$(blkid -s UUID -o value "$dev" 2>/dev/null || true)
    fstype=$(blkid -s TYPE -o value "$dev" 2>/dev/null || true)
    [[ -z "$uuid" ]] && die "$dev has no filesystem UUID — format it first (try mode 3)"
    [[ -z "$fstype" ]] && die "$dev has no recognized filesystem"
    log "found $dev: UUID=$uuid TYPE=$fstype"

    local mountpoint
    mountpoint=$(ask "Where to mount $dev" "/mnt/ftd-backup")
    mkdir -p "$mountpoint"

    local already_mounted
    already_mounted=$(findmnt -no TARGET --source "UUID=$uuid" 2>/dev/null || true)
    if [[ -n "$already_mounted" && "$already_mounted" != "$mountpoint" ]]; then
        if confirm "$dev is already mounted at $already_mounted. Unmount and remount at $mountpoint?" "n"; then
            umount "$already_mounted" || die "could not umount $already_mounted"
        else
            mountpoint="$already_mounted"
            log "keeping existing mountpoint: $mountpoint"
        fi
    fi

    if ! grep -qE "^UUID=$uuid[[:space:]]" /etc/fstab; then
        echo "UUID=$uuid  $mountpoint  $fstype  defaults,noatime,nofail  0  2" >> /etc/fstab
        ok "added fstab entry for UUID=$uuid"
    else
        log "fstab entry for UUID=$uuid already present, leaving as-is"
    fi
    systemctl daemon-reload || true
    if ! findmnt -no TARGET "$mountpoint" >/dev/null 2>&1; then
        mount "$mountpoint" || die "mount $mountpoint failed"
    fi
    STORAGE_UNDERLYING="$mountpoint"
    ok "$dev mounted at $mountpoint"
}

# _mode3_format_disk — wipe + format a whole disk, mount, bind.
_mode3_format_disk() {
    local -a d_names=() d_sizes=() d_models=() d_serials=()
    local name short risky

    # Query names only first (avoids IFS/space issues with multi-word model strings).
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
        d_serials+=("$(lsblk -dno SERIAL "$name" | xargs || echo '?')")
    done < <(lsblk -dpno NAME)

    [[ ${#d_names[@]} -eq 0 ]] && die "no eligible whole disks found (system disk and mounted disks excluded)"

    {
        echo
        echo "${BOLD}Eligible whole disks (system disk and mounted disks excluded):${RESET}"
        local i
        for i in "${!d_names[@]}"; do
            printf '  %s%d)%s %-15s  %-8s  %-28s  %s\n' \
                "$BOLD" "$((i+1))" "$RESET" \
                "${d_names[$i]}" "${d_sizes[$i]}" "${d_models[$i]}" "${d_serials[$i]}"
        done
        echo
    } >&2

    local reply dev
    while true; do
        read -r -p "${BOLD}Disk to format${RESET} [1-${#d_names[@]}]: " reply </dev/tty
        if [[ "$reply" =~ ^[0-9]+$ ]] && (( reply >= 1 && reply <= ${#d_names[@]} )); then
            dev="${d_names[$((reply-1))]}"; break
        fi
        warn "enter a number between 1 and ${#d_names[@]}"
    done
    local size model serial
    size=$(lsblk -dno SIZE "$dev")
    model=$(lsblk -dno MODEL "$dev" | xargs)
    serial=$(lsblk -dno SERIAL "$dev" | xargs)

    echo
    echo "${RED}${BOLD}WARNING:${RESET} this will ${RED}DESTROY ALL DATA${RESET} on:"
    echo "  device : $dev"
    echo "  size   : $size"
    echo "  model  : $model"
    echo "  serial : $serial"
    echo
    [[ -z "$serial" ]] && die "cannot identify disk serial — refusing to format without a stable identifier"
    if ! ask_typed_match "Type the disk SERIAL to confirm" "$serial"; then
        die "serial mismatch — aborted"
    fi

    log "creating GPT label and single ext4 partition on $dev"
    parted -s "$dev" mklabel gpt
    parted -s -a optimal "$dev" mkpart primary ext4 0% 100%
    # Re-read partition table; settle udev.
    partprobe "$dev"
    udevadm settle

    # Find the new partition (handle both nvme1n1p1 and sda1 naming)
    local part
    part=$(lsblk -rnpo NAME "$dev" | awk -v d="$dev" '$1 != d {print; exit}')
    [[ -b "$part" ]] || die "could not locate new partition on $dev"
    log "formatting $part as ext4 (label=ftd-backup)"
    mkfs.ext4 -F -L ftd-backup "$part"

    local uuid
    uuid=$(blkid -s UUID -o value "$part")
    [[ -n "$uuid" ]] || die "no UUID after mkfs"

    local mountpoint="/mnt/ftd-backup"
    mkdir -p "$mountpoint"
    if ! grep -qE "^UUID=$uuid[[:space:]]" /etc/fstab; then
        echo "UUID=$uuid  $mountpoint  ext4  defaults,noatime,nofail  0  2" >> /etc/fstab
    fi
    systemctl daemon-reload || true
    mount "$mountpoint" || die "mount $mountpoint failed"
    STORAGE_UNDERLYING="$mountpoint"
    ok "formatted $part (UUID=$uuid), mounted at $mountpoint"
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

    # Final write test on the canonical app-facing path.
    touch "$STORAGE_BIND/.ftd-writetest" || die "$STORAGE_BIND is not writable"
    rm -f "$STORAGE_BIND/.ftd-writetest"
    ok "$STORAGE_BIND is mounted and writable"
}

choose_storage() {
    _show_block_devices
    echo "${BOLD}How should backups be stored?${RESET}"
    echo "  1) Use an existing directory or already-mounted path"
    echo "  2) Adopt an existing partition (read UUID, add fstab, mount)"
    echo "  3) Format a fresh whole disk (DESTRUCTIVE — confirms by serial)"
    echo
    local choice
    while true; do
        choice=$(ask "Choose 1/2/3" "1")
        case "$choice" in
            1) _mode1_existing_mount; break ;;
            2) _mode2_adopt_partition; break ;;
            3) _mode3_format_disk; break ;;
            *) warn "invalid choice: $choice" ;;
        esac
    done
    _setup_bind_mount
}
