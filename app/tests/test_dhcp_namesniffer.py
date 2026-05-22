import struct
from app import dhcp_namesniffer as sn


def build_dhcp_request_with_hostname(hostname: bytes, mac: bytes, include_hostname: bool = True) -> bytes:
    """Build a minimal DHCPREQUEST payload (op=BOOTREQUEST, with optional option 12)."""
    # op=1 (BOOTREQUEST), htype=1, hlen=6, hops=0
    header = bytes([1, 1, 6, 0])
    xid = b"\x00\x00\x00\x01"
    secs_flags = b"\x00\x00\x80\x00"  # broadcast flag
    ciaddr = b"\x00\x00\x00\x00"
    yiaddr = bytes([10, 0, 0, 42])
    siaddr = b"\x00\x00\x00\x00"
    giaddr = b"\x00\x00\x00\x00"
    chaddr = mac + b"\x00" * (16 - len(mac))
    sname = b"\x00" * 64
    file_ = b"\x00" * 128
    magic = sn.DHCP_MAGIC
    if include_hostname:
        options = bytes([53, 1, 3, 12, len(hostname)]) + hostname + bytes([0xff])
    else:
        options = bytes([53, 1, 3, 0xff])
    return header + xid + secs_flags + ciaddr + yiaddr + siaddr + giaddr + chaddr + sname + file_ + magic + options


def test_parse_dhcp_extracts_hostname_and_mac_and_ip():
    mac = bytes([0xaa, 0xbb, 0xcc, 0x11, 0x22, 0x33])
    payload = build_dhcp_request_with_hostname(b"WIN10-PC01", mac)

    result = sn.parse_dhcp(payload)

    assert result is not None
    parsed_mac, parsed_host, parsed_ip = result
    assert parsed_mac == "aa:bb:cc:11:22:33"
    assert parsed_host == "WIN10-PC01"
    assert parsed_ip == "10.0.0.42"


def test_parse_dhcp_returns_none_for_packet_without_option_12():
    mac = bytes([0xaa, 0xbb, 0xcc, 0x11, 0x22, 0x33])
    payload = build_dhcp_request_with_hostname(b"", mac, include_hostname=False)
    assert sn.parse_dhcp(payload) is None


def test_parse_dhcp_rejects_truncated_payload():
    assert sn.parse_dhcp(b"\x00" * 50) is None


def test_parse_dhcp_rejects_invalid_hostname():
    mac = bytes([0xaa, 0xbb, 0xcc, 0x11, 0x22, 0x33])
    # Hostname with HTML/control chars — should be rejected by _VALID_HOSTNAME_RE
    payload = build_dhcp_request_with_hostname(b"<script>", mac)
    assert sn.parse_dhcp(payload) is None


def test_parse_nbns_still_works():
    # Smoke check that splitting didn't break the surviving function.
    # Build a NBNS registration request with workstation name "TESTBOX".
    flags = (5 << 11)  # qr=0, opcode=5
    header = struct.pack("!HHHHHH", 0x4242, flags, 1, 0, 0, 0)
    raw = b"TESTBOX" + b" " * (15 - 7) + b"\x00"
    encoded = bytearray()
    encoded.append(0x20)
    for b in raw:
        encoded.append(ord("A") + (b >> 4))
        encoded.append(ord("A") + (b & 0x0F))
    encoded.append(0x00)  # null-terminator of name
    encoded += struct.pack("!HH", 0x0020, 0x0001)
    payload = header + bytes(encoded)
    result = sn.parse_nbns(payload)
    assert result is not None
    name, opcode = result
    assert name == "TESTBOX"
    assert opcode == 5
