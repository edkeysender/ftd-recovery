#!/bin/bash
MAC=$(cat /sys/class/net/$(ip route show default | awk '/default/ {print $5}')/address | tr ':' '-')
MAC_COLONS=$(echo "$MAC" | tr '-' ':')
API="http://__SERVER_IP__:8088"

# Disarm immediately. A forced reboot mid-restore would otherwise PXE straight
# back into restore. The end-of-script disarm below stays as a safety net.
curl -fsS -m 5 -X DELETE "${API}/api/arm/${MAC_COLONS}" >/dev/null 2>&1 || true

# Image to restore: kernel cmdline 'ocs_image=<name>' wins; else newest img-<MAC>-*
IMG=$(cat /proc/cmdline | tr ' ' '\n' | sed -nE 's/^ocs_image=([A-Za-z0-9._-]+)$/\1/p' | head -1)
if [ -z "$IMG" ]; then
    LATEST=$(ls -td /home/partimag/img-${MAC}-* 2>/dev/null | head -1)
    if [ -n "$LATEST" ]; then
        IMG=$(basename "$LATEST")
    else
        IMG="img-${MAC}"
    fi
fi

# Find the primary internal disk
DISK=""
for d in $(lsblk -ndo NAME,TYPE | awk '$2=="disk"{print $1}'); do
    [[ "$d" == loop* ]] && continue
    [[ "$d" == zram* ]] && continue
    [[ "$d" == ram* ]] && continue
    [ "$(cat /sys/block/$d/removable 2>/dev/null)" = "1" ] && continue
    readlink -f /sys/block/$d | grep -q usb && continue
    SIZE=$(cat /sys/block/$d/size 2>/dev/null)
    [ -z "$SIZE" ] || [ "$SIZE" = "0" ] && continue
    DISK="$d"
    break
done

if [ -z "$DISK" ]; then
    echo "ERROR: No internal disk found!"
    lsblk -o NAME,SIZE,TYPE,TRAN,MODEL
    curl -fsS -m 5 -X DELETE "${API}/api/arm/${MAC_COLONS}" >/dev/null 2>&1 || true
    read -p "Press Enter to reboot..." _
    reboot
fi

DISK_SIZE=$(lsblk -ndo SIZE /dev/$DISK)
DISK_MODEL=$(lsblk -ndo MODEL /dev/$DISK)

echo "============================================"
echo " FTD.AERO Recovery Center - RESTORE"
echo "============================================"
echo " MAC:   ${MAC}"
echo " Image: ${IMG}"
echo " Disk:  /dev/${DISK} (${DISK_SIZE} ${DISK_MODEL})"
echo "============================================"
echo ""

if [ ! -d "/home/partimag/${IMG}" ]; then
    echo "ERROR: image '${IMG}' not found under /home/partimag/"
    echo ""
    echo "Available images:"
    ls -d /home/partimag/img-* /home/partimag/*-img 2>/dev/null || echo "  (none)"
    echo ""
    curl -fsS -m 5 -X DELETE "${API}/api/arm/${MAC_COLONS}" >/dev/null 2>&1 || true
    read -p "Press Enter to reboot..." _
    reboot
fi

PROGRESS_URL="${API}/api/progress/${MAC_COLONS}"
PROGRESS_LOG=$(mktemp /tmp/ocs-restore-XXXXXX.log)

post_progress() {
    curl -fsS -m 2 -X POST "$PROGRESS_URL" \
        -H 'Content-Type: application/json' \
        --data "$1" >/dev/null 2>&1 || true
}

post_progress '{"phase":"restore","percent":0,"status":"started"}'

(
    while sleep 2; do
        LINE=""
        if [ -s "$PROGRESS_LOG" ]; then
            LINE=$(tac "$PROGRESS_LOG" | tr '\r' '\n' | grep -m1 -E 'Completed:[[:space:]]*[0-9.]+%' || true)
        fi
        if [ -z "$LINE" ]; then
            post_progress '{"phase":"restore","status":"running"}'
            continue
        fi
        PCT=$(echo "$LINE"     | sed -nE 's/.*Completed:[[:space:]]*([0-9.]+)%.*/\1/p')
        ELAPSED=$(echo "$LINE" | sed -nE 's/.*Elapsed:[[:space:]]*([0-9:]+).*/\1/p')
        REMAIN=$(echo "$LINE"  | sed -nE 's/.*Remaining:[[:space:]]*([0-9:]+).*/\1/p')
        RATE=$(echo "$LINE"    | sed -nE 's/.*Rate:[[:space:]]*([^,[:space:]]+).*/\1/p')
        post_progress "{\"phase\":\"restore\",\"status\":\"running\",\"percent\":${PCT:-0},\"elapsed\":\"${ELAPSED}\",\"eta\":\"${REMAIN}\",\"rate\":\"${RATE}\"}"
    done
) &
REPORTER_PID=$!

/usr/sbin/ocs-sr -g auto -e1 auto -e2 -r -j2 -scr restoredisk "$IMG" "$DISK" 2>&1 | tee "$PROGRESS_LOG"
RC=${PIPESTATUS[0]}

kill "$REPORTER_PID" 2>/dev/null
wait "$REPORTER_PID" 2>/dev/null

if [ "$RC" = "0" ]; then
    post_progress '{"phase":"completed","status":"completed","percent":100}'
else
    post_progress "{\"phase\":\"failed\",\"status\":\"failed\",\"rc\":$RC}"
fi

# Disarm so the next boot doesn't loop straight back into restore
curl -fsS -m 5 -X DELETE "${API}/api/arm/${MAC_COLONS}" >/dev/null 2>&1 || true

reboot
