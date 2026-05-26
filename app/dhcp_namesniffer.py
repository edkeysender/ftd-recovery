#!/usr/bin/env python3
"""Hostname sniffer: passive DHCP + NetBIOS Name Service capture.

Listens on eth0 (raw + promisc) for two passive sources of Windows/Linux
hostnames and writes them to a JSON cache the recovery UI reads during
/api/scan:

  - DHCP Option 12 (sent by Windows on boot / ipconfig /renew)
  - NetBIOS NBNS name registration/refresh broadcasts on UDP 137
    (every Windows machine periodically reclaims its computer name)

Both are broadcast, so a managed switch still floods them to our port.
"""
import json
import os
import re
import signal
import socket
import struct
import sys
import time
from pathlib import Path

_VALID_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,62}$")

CACHE_FILE = Path(__file__).resolve().parent / "dhcp_names.json"
INTERFACE = os.environ.get("DHCP_SNIFF_IFACE", "eth0")
SAVE_INTERVAL = 5.0

ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800
DHCP_MAGIC = b"\x63\x82\x53\x63"

SOL_PACKET = 263
PACKET_ADD_MEMBERSHIP = 1
PACKET_MR_PROMISC = 1


def enable_promisc(sock: socket.socket, ifname: str) -> None:
    ifindex = socket.if_nametoindex(ifname)
    # struct packet_mreq { int mr_ifindex; ushort mr_type; ushort mr_alen; uchar mr_address[8]; }
    mreq = struct.pack("iHH8s", ifindex, PACKET_MR_PROMISC, 0, b"")
    sock.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)


def mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def decode_netbios_name(encoded: bytes):
    """Decode a 32-byte first-level-encoded NetBIOS name. Returns (name, suffix) or None."""
    if len(encoded) != 32:
        return None
    decoded = bytearray()
    for i in range(0, 32, 2):
        hi = encoded[i] - 0x41
        lo = encoded[i + 1] - 0x41
        if not (0 <= hi < 16 and 0 <= lo < 16):
            return None
        decoded.append((hi << 4) | lo)
    name = bytes(decoded[:15]).rstrip(b" \x00").decode("ascii", errors="replace").strip()
    return name, decoded[15]


def parse_nbns(payload: bytes):
    """Parse NBNS packet, return (workstation_name, opcode_name) or None.

    Only returns a name when:
      - QR=0 (it's a request from a client, not a response)
      - The first question / additional record is encoded as Workstation (<00>)
    """
    if len(payload) < 12 + 1 + 32 + 1 + 4:
        return None
    flags = struct.unpack("!H", payload[2:4])[0]
    qr = (flags >> 15) & 1
    if qr != 0:
        return None
    opcode = (flags >> 11) & 0xF
    # Accept only registration/refresh, where the question name is the
    # client's own computer name. Query (0) often carries wildcard "*" or
    # arbitrary lookups — too noisy.
    if opcode not in (5, 8, 9, 15):
        return None
    # Question section starts at offset 12. First byte is the length (0x20=32).
    if payload[12] != 0x20:
        return None
    decoded = decode_netbios_name(payload[13:45])
    if not decoded:
        return None
    name, suffix = decoded
    if suffix != 0x00 or not _VALID_HOSTNAME_RE.match(name):
        return None
    return name, opcode


def parse_dhcp(payload: bytes):
    """Parse DHCP payload, return (mac, hostname, ip) or None."""
    if len(payload) < 240 or payload[2] != 6:
        return None
    if payload[236:240] != DHCP_MAGIC:
        return None
    chaddr = payload[28:34]
    hostname = None
    yiaddr = ".".join(str(b) for b in payload[16:20])
    ciaddr = ".".join(str(b) for b in payload[12:16])
    opts = payload[240:]
    i = 0
    n = len(opts)
    while i < n:
        code = opts[i]
        if code == 0xff:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= n:
            break
        olen = opts[i + 1]
        if i + 2 + olen > n:
            break
        if code == 12:
            try:
                hostname = opts[i + 2:i + 2 + olen].decode("ascii", errors="replace").strip("\x00").strip()
            except Exception:
                hostname = None
        i += 2 + olen
    if not hostname:
        return None
    if not _VALID_HOSTNAME_RE.match(hostname):
        return None
    ip = yiaddr if yiaddr != "0.0.0.0" else (ciaddr if ciaddr != "0.0.0.0" else None)
    return mac_str(chaddr), hostname, ip


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
    os.chmod(tmp, 0o644)
    tmp.replace(CACHE_FILE)


def main() -> int:
    cache = load_cache()
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sock.bind((INTERFACE, 0))
    try:
        enable_promisc(sock, INTERFACE)
        promisc = "promisc"
    except OSError as e:
        promisc = f"no-promisc ({e})"
    print(f"[dhcp-sniffer] listening on {INTERFACE} [{promisc}]", flush=True)

    stop = False

    def handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    last_save = time.time()
    dirty = False

    while not stop:
        try:
            pkt, _ = sock.recvfrom(65535)
        except OSError:
            continue
        # Minimum: ETH(14) + IP(20) + UDP(8) + at least 1 byte = 43
        if len(pkt) < 43:
            continue
        if struct.unpack("!H", pkt[12:14])[0] != ETH_P_IP:
            continue
        ip_hdr = pkt[14:34]
        ihl = (ip_hdr[0] & 0x0f) * 4
        if ihl < 20 or ip_hdr[9] != 17:
            continue
        udp_off = 14 + ihl
        if len(pkt) < udp_off + 8:
            continue
        sport, dport = struct.unpack("!HH", pkt[udp_off:udp_off + 4])
        payload = pkt[udp_off + 8:]
        src_mac = mac_str(pkt[6:12])
        src_ip = ".".join(str(b) for b in ip_hdr[12:16])

        mac = hostname = ip = source = None
        if 67 in (sport, dport) or 68 in (sport, dport):
            r = parse_dhcp(payload)
            if r:
                mac, hostname, ip = r
                source = "dhcp"
        elif 137 in (sport, dport):
            r = parse_nbns(payload)
            if r:
                hostname, _ = r
                mac = src_mac
                ip = src_ip if src_ip != "0.0.0.0" else None
                source = "nbns"

        if not hostname or not mac or mac == "00:00:00:00:00:00":
            continue

        entry = cache.get(mac, {})
        prev = entry.get("hostname")
        # DHCP-sourced names win over NBNS (NBNS truncates to 15 chars).
        if prev and entry.get("source") == "dhcp" and source == "nbns":
            # Refresh last_seen but don't overwrite a richer DHCP name with truncated NBNS.
            entry["last_seen"] = int(time.time())
            if ip:
                entry["last_ip"] = ip
            cache[mac] = entry
            dirty = True
        else:
            entry["hostname"] = hostname
            entry["source"] = source
            entry["last_seen"] = int(time.time())
            if ip:
                entry["last_ip"] = ip
            cache[mac] = entry
            if prev != hostname:
                print(f"[sniffer] {mac} -> {hostname} (via {source}, ip={ip})", flush=True)
            dirty = True

        now = time.time()
        if dirty and now - last_save > SAVE_INTERVAL:
            save_cache(cache)
            last_save = now
            dirty = False

    if dirty:
        save_cache(cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
