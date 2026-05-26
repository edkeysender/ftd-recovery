import asyncio
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Literal, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).parent
HOSTS_FILE = APP_DIR / "hosts.yml"
STATE_FILE = APP_DIR / "state.json"
DHCP_NAMES_FILE = APP_DIR / "dhcp_names.json"
IPXE_DIR = Path("/srv/tftp")
ALLOWLIST_HELPER = "/usr/local/bin/recovery-allowlist"
BACKUP_STORAGE = "/srv/clonezilla-images"

_SAFE_NAME_PAT = r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$"
_SAFE_NAME_RE = re.compile(_SAFE_NAME_PAT)


def _safe_name(candidate) -> str:
    """Return the input if it matches our hostname regex, else empty string."""
    if not candidate:
        return ""
    candidate = str(candidate).strip()
    return candidate if _SAFE_NAME_RE.match(candidate) else ""


def last_backup_at(mac: Optional[str]) -> Optional[float]:
    """Latest mtime across /srv/clonezilla-images/img-<MAC>{,-<timestamp>}/, or None."""
    if not mac:
        return None
    base = Path(BACKUP_STORAGE)
    if not base.is_dir():
        return None
    prefix = f"img-{mac.replace(':', '-')}"
    latest = 0.0
    try:
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            if entry.name == prefix or entry.name.startswith(f"{prefix}-"):
                try:
                    m = entry.stat().st_mtime
                    if m > latest:
                        latest = m
                except OSError:
                    continue
    except OSError:
        return None
    return latest if latest > 0 else None

PING_TIMEOUT = 1
HOSTNAME_TTL = 300
ARM_TTL = 300  # 5 minutes
PROGRESS_TTL = 120  # progress entries older than this are stale and hidden
INTERFACE = os.environ.get("RECOVERY_IFACE", "eth0")

Mode = Literal["recovery", "backup"]
MODE_TO_IPXE_FILE = {
    "recovery": "boot-restore.ipxe",
    "backup": "boot-backup.ipxe",
}

IPXE_LOCAL = "#!ipxe\necho No recovery mode armed for ${mac}. Booting local disk.\nexit\n"

GRUBCFG_HELPER = "/usr/local/bin/recovery-grubcfg"

def write_grub_armed(mac: str, mode: str, image: Optional[str] = None) -> None:
    """Write the per-MAC grub.cfg. Raises CalledProcessError on failure — callers handle rollback."""
    cmd = ["sudo", "-n", GRUBCFG_HELPER, "write", mac, mode]
    if image:
        cmd.append(image)
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=10)

def remove_grub_armed(mac: str) -> None:
    try:
        subprocess.run(["sudo", "-n", GRUBCFG_HELPER, "remove", mac],
                       check=True, capture_output=True, text=True, timeout=10)
    except subprocess.CalledProcessError as e:
        print(f"[disarm] grubcfg remove failed for {mac}: {e.stderr.strip() or e}")

app = FastAPI(title="Recovery Status")

_hostname_cache: dict[str, tuple[float, Optional[str]]] = {}


# ---------- hosts.yml ----------

def load_hosts() -> list[dict]:
    if not HOSTS_FILE.exists():
        return []
    data = yaml.safe_load(HOSTS_FILE.read_text()) or {}
    return data.get("hosts", [])


def save_hosts(hosts: list[dict]) -> None:
    HOSTS_FILE.write_text(yaml.safe_dump({"hosts": hosts}, sort_keys=False))


# DHCP hostname cache written by recovery-dhcp-sniffer.service
# Auto-names: "Unknown-1a2b" or vendor-tagged like "Intel-1a2b", "TP-Link-89d2".
_AUTO_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,20}-[0-9a-f]{4}$")


def load_dhcp_names() -> dict[str, str]:
    """Return {normalized_mac: hostname} from the sniffer cache."""
    if not DHCP_NAMES_FILE.exists():
        return {}
    try:
        raw = json.loads(DHCP_NAMES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, str] = {}
    for mac, entry in raw.items():
        try:
            mac_n = normalize_mac(mac)
        except ValueError:
            continue
        hostname = (entry or {}).get("hostname")
        if hostname:
            out[mac_n] = hostname
    return out


def is_auto_name(name: str) -> bool:
    return bool(_AUTO_NAME_RE.match(name or ""))


# ---------- state.json (armed MACs) ----------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"armed": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {"armed": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def prune_expired(state: dict) -> tuple[dict, list[str]]:
    now = time.time()
    expired = [mac for mac, e in state["armed"].items() if e["expires_at"] <= now]
    for mac in expired:
        del state["armed"][mac]
        try:
            run_allowlist("remove", mac)
        except Exception as exc:
            print(f"[prune] failed to remove {mac} from allowlist: {exc}")
        remove_grub_armed(mac)
    return state, expired


# ---------- helpers ----------

def run_allowlist(action: str, mac: str) -> None:
    subprocess.run(
        ["sudo", "-n", ALLOWLIST_HELPER, action, mac],
        check=True, capture_output=True, text=True, timeout=10,
    )


async def ping_host(host: str) -> tuple[bool, Optional[float]]:
    proc = await asyncio.create_subprocess_exec(
        "ping", "-c", "1", "-W", str(PING_TIMEOUT), host,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return False, None
    for line in out.decode(errors="ignore").splitlines():
        if "time=" in line:
            try:
                return True, float(line.split("time=")[1].split()[0])
            except (IndexError, ValueError):
                return True, None
    return True, None


async def resolve_hostname(ip: str) -> Optional[str]:
    now = time.time()
    cached = _hostname_cache.get(ip)
    if cached and now - cached[0] < HOSTNAME_TTL:
        return cached[1]
    loop = asyncio.get_event_loop()
    try:
        name = (await loop.run_in_executor(None, socket.gethostbyaddr, ip))[0]
    except (socket.herror, socket.gaierror, OSError):
        name = None
    _hostname_cache[ip] = (now, name)
    return name


async def arp_lookup(ip: str) -> Optional[str]:
    proc = await asyncio.create_subprocess_exec(
        "ip", "neigh", "show", ip,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    parts = out.decode(errors="ignore").split()
    if "lladdr" in parts:
        i = parts.index("lladdr")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def normalize_mac(mac: str) -> str:
    h = "".join(c for c in mac.lower() if c.isalnum())
    if len(h) != 12 or any(c not in "0123456789abcdef" for c in h):
        raise ValueError(f"invalid MAC: {mac!r}")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


# ---------- OUI classifier ----------
# Hand-curated OUI prefixes (no separators, lowercase) for the most common
# non-PC vendors seen on office/lab LANs. The goal is high precision on the
# "nonpc" verdict — we'd rather leave a device unclassified than hide a PC.

_NONPC_OUIS = {
    # Espressif (ESP32/ESP8266 IoT) — 30f6ef is Intel per IEEE, not Espressif
    "7c9ebd", "ec6260", "24b2de", "840d8e", "240ac4", "a020a6",
    "8caab5", "c44f33", "30aea4", "9c9c1f", "246f28", "3c71bf", "ac67b2",
    "e09806", "f4cfa2", "94b97e", "c8c9a3", "08b61f", "083af2", "5443b2",
    # Sonos
    "7828ca", "000e58", "5caafd", "949f3e", "b8e937", "48a6b8", "94ce31",
    # Brother (printer)
    "008077", "30055c", "30c01b", "4cfcaa", "040e3c", "ac44f2", "001ba9",
    "008092", "30c9ab",
    # Canon (printer/scanner)
    "e0e1a9", "609ec8", "001e8f", "c899b2", "500b32", "5cf9db", "0080a3",
    "001585", "30853b",
    # Epson Seiko (printer)
    "000048", "28e7cf", "90489a", "78e3fb", "64eb8c", "9caed3", "0026ab",
    "44d244", "a4ee57", "fcaa14",
    # Xerox / Fuji Xerox (printer)
    "000000", "0000aa", "9c934e", "0800f0",  # 000000 dropped below
    # Kyocera (printer)
    "00c0ee", "001ddf", "00179a",
    # Ricoh (printer)
    "000074", "0026731", "002673",
    # Lexmark (printer)
    "00040e", "00219b", "002564", "f8b156",
    # Zebra (label printer)
    "00074d", "0007ee", "002a73", "94f6d6",
    # HP printers (Inc.) — overlap risk with HP PCs, but these blocks are
    # used predominantly by the printer division per public OUI listings.
    "002655", "0017a4", "002481", "9c8e99",  # leave HP PC blocks out
    # Cisco / Cisco Meraki (network gear)
    "001f9d", "001e79", "00211c", "00211b", "00235e", "002584", "00260b",
    "00260a", "000ab8", "000bfd", "1c6a7a", "881544", "ac7e8a", "ec3091",
    "881544", "00179b", "0017df", "001bd4", "001c0e", "0c8126", "ccef48",
    "e4c722", "f8a5c5", "00224d", "0023ac",
    # Cisco-Linksys (older home routers, also some VoIP)
    "08cc81", "001a70", "001839", "0018f8",
    # Ubiquiti (APs / switches)
    "00156d", "245a4c", "24a43c", "44d9e7", "78458c", "802aa8", "fcecda",
    "f09fc2", "682c7b", "b4fbe4", "dc9fdb", "e063da",
    # MikroTik
    "4c5e0c", "6c3b6b", "742f68", "b869f4", "cc2de0", "d4ca6d", "e48d8c",
    # TP-Link (consumer routers / smart home)
    "001950", "002586", "1027f5", "14ebb6", "30b5c2", "5c628b", "60e327",
    "984848", "b0487a", "c46e1f", "d80d17", "ec086b", "f0f7c4",
    # D-Link
    "00179a", "001cf0", "002191", "00226b", "002401", "002a8a", "1c5f2b",
    "5cd998", "78542e", "ccb255", "f48e38",
    # Netgear
    "00146c", "001b2f", "001e2a", "00223f", "08bd43", "10da43", "204e7f",
    "289401", "2c308b", "30460f", "443719", "4c60de", "744401", "9c3dcf",
    # Aruba Networks (HPE) — APs
    "001a1e", "000b86", "94b40f", "ac1e08", "208984", "9020c2", "d8c7c8",
    # Hikvision (camera)
    "047f0e", "2857be", "4419b6", "5850f2", "8ce748", "c0517e", "c42f90",
    "ecc89c", "f0aff2", "f84dfc", "44478b", "bca94a",
    # Dahua (camera)
    "14a78b", "24526a", "38af29", "3cef8c", "4c11bf", "64db8b", "9002a9",
    "a0bd1d", "4cbd8f", "9c14637", "9c1463",
    # Axis (camera)
    "00408c", "accc8e", "b8a44f",
    # Polycom / Yealink / Grandstream (VoIP phones)
    "0004f2", "001956", "08006b", "640e36", "805ec0", "8064e8", "245408",
    "000b82", "0021f7", "0c1105", "ec74d7",  # Grandstream / Yealink
    # Amazon (Echo / Fire) — definitely not PCs we'd image
    "0c47c9", "1840df", "34d270", "44650d", "4cefc0", "503da1", "684a64",
    "881ed8", "a002dc", "a8004e", "ac633e", "b47c9c", "f0d2f1", "fcc73a",
    # Google / Nest
    "001a11", "1c3947", "404a18", "489674", "54600c", "6466b3", "9c2e76",
    "a4677c", "d04f7e", "e8eada", "f4f5d8",
    # Roku
    "0c1530", "8c4962", "ac3a7a", "b083fe", "b8a175", "c8db26", "d4cbaf",
    "d83134", "dca632", "ddfa6c",
    # Sonos already above
    # Honeywell / building automation
    "00d004", "00d038", "002409", "0017f1",
    # Tuya / Xiaomi IoT
    "d8f15b", "dc4f22", "50ec50", "70bb1e", "5cf64c", "78a351", "8caab5",
    # Philips Hue / Signify
    "0017889", "001788", "ec1bbd", "00178899",  # cleaned to 6 below
}
# Strip any entries that aren't exactly 6 hex chars (typos in the source above).
_NONPC_OUIS = {x for x in _NONPC_OUIS if len(x) == 6 and all(c in "0123456789abcdef" for c in x)}
# 00:00:00 is the "null" OUI / loopback — never auto-hide on this.
_NONPC_OUIS.discard("000000")

# PC NIC vendors. Used only for the "pc" category badge (informational);
# nothing is auto-hidden based on this list.
_PC_OUIS = {
    # Intel Corporate (sample of the busiest blocks — Intel has hundreds)
    "001500", "001b21", "001e64", "00216a", "0022fa", "00269e", "001f3c",
    "0050ba", "0c8bfd", "1c697a", "1cbfce", "240a64", "28b2bd", "2c6e85",
    "3c970e", "4c34889", "4c3488", "5cf9dd", "606720", "688f84", "705a0f",
    "7c5cf8", "80fa5b", "8c1645", "9c2a83", "a08869", "a4bf01", "b0359f",
    "b496913", "b49691", "c48508", "c8f750", "d8fc93", "e4a471", "f8e43b",
    # Dell
    "001143", "0014228", "001422", "0015c5", "0018f3", "0018b2", "001cf0",
    "00219b", "002219", "00248c", "00261805", "002618", "00b0d0", "00c04f",
    "001ea4", "002564", "00188b", "001c23", "001e4f", "002170", "002564",
    "5cf9dd", "78458c", "a41f72", "b083fe", "b8ca3a", "d4ae52", "ec5c69",
    "f8bc12", "f8db884", "f8db88", "f8cab8",
    # Lenovo
    "0021cc", "002564", "00595b", "00505bb", "00ff20", "1002b5", "147582",
    "1c1b0d", "1c4d70", "1c75083", "1c7508", "2c337a", "30b49e", "3cf011",
    "4ccc6a", "5811220", "581122", "6c0b84", "6c5f1c", "8cdcd4", "98d6f7",
    "a4170e", "bc83a7", "c87f54", "ccb0da", "d04a55", "d04f7e",
    # HP Inc PC division
    "00086d", "0023c8", "0026ae", "00306e", "002264", "001321", "001438",
    "001819", "001b78", "001ee5", "002170", "1cc1de", "2c44fd", "2c768a",
    "2c41388", "2c4138", "308d99", "3413e8", "38eaa7", "3c2c30", "405cfd",
    "5cb901", "646a52", "8851fb", "94c691", "9c8e99", "a45e60", "b499ba",
    # ASUSTek
    "00088a", "000c6e", "000ea6", "0013d4", "00179a", "001999", "001bfc",
    "001d60", "001ee8", "0022150", "002215", "002354", "0024d2", "00266f",
    "002354", "00e018", "10c37b", "1c872c", "30850a", "381a52", "40b076",
    "44a191", "48ee0c", "501ac56", "5404a6", "60a44c", "6c626d", "704d7b",
    "7824af", "ac220b", "b06ebf", "bcaec5", "c860006", "c86000", "d017c2",
    # Gigabyte
    "001fd0", "00216b", "002354", "002618", "0050ba", "1c4bd6", "1c6f65",
    "30deea", "3417eb", "4c52623", "4c5262", "50e549", "5404a6", "70de31",
    "94de80", "94ddf8", "a8a159", "ace2d3", "b06ebf", "bc5ff4", "d050996",
    "d05099", "d4a425", "ec8eb5", "f02f74", "f4b520",
    # MSI / Micro-Star
    "00163e", "001afd", "002354", "0021850", "002185", "0022380", "002238",
    "002354", "00269e", "00306e", "0c6a8f", "30055c", "30b5c2", "3859f9",
    "5404a6", "8c89a5", "a4bb6d", "b07b25", "b8c620", "d017c2", "d4ae52",
    # Realtek (used by most cheap motherboard NICs)
    "0010188", "001018", "00e04c", "527ec1", "525400", "5254ab", "5254bf",
    # ASRock
    "94de80",
    # Supermicro
    "00259003", "002590", "002590", "0030480", "003048", "0cc47a", "1402ec",
    "3cecef", "ac1f6b", "b8aeed", "d05099",
    # Hon Hai / Foxconn (most laptop ODM)
    "0016cf", "0017f2", "001839", "001b24", "001cc0", "002080", "002241",
    "002522", "0023061", "002306", "00248c", "00254b", "0026370", "002637",
    "00266b", "0090f5", "08ed02", "0c8268", "1080123", "108012", "20cf30",
    "20689d4", "20689d", "30f9ed", "382c4a", "3c970e", "44877f", "489674",
    "4ccc6a", "60020a", "744401", "7427ea", "788a20", "8086f2", "885af8",
    "a486375", "a48637", "a834d5", "d4ad20", "e0db55", "f0b428", "f8d111",
    # Liteon (laptops/wifi)
    "00229f", "002564", "00237d", "002566", "00266c", "002659", "00269e",
    "00410b41", "00410b", "10683f", "1c659d", "1ccae3", "284c53", "30b49e",
    "30855a", "3cf86e", "4486a1", "5404a6", "5ce0c5", "60a44c", "64bc0c",
    "6c0b840", "6c0b84", "744401", "80e650", "84ef18", "88a29e", "9cb6d0",
    "a0ad9f", "a45e60", "b0359f", "b832e5", "d850e6", "ec55f9", "fcaa14",
    # AMD (used on some recent motherboards)
    "00098f", "001124", "0015fe", "0023e9", "847b57", "a4c494",
    # Apple — Macs are PCs we could conceivably image, but they're also iPads
    # and iPhones. Skipping here keeps the badge meaning specific to Win/Linux.
}
_PC_OUIS = {x for x in _PC_OUIS if len(x) == 6 and all(c in "0123456789abcdef" for c in x)}


def _oui(mac: str) -> str:
    """Return normalized 6-char OUI (lowercase hex) from a MAC string."""
    return mac.replace(":", "").replace("-", "").lower()[:6]


def _is_locally_administered(oui: str) -> bool:
    """Locally-administered MACs have bit 1 of the first octet set — these
    are randomized addresses (Windows MAC randomization, Apple privacy MAC)
    and convey no vendor info."""
    try:
        first = int(oui[:2], 16)
    except (ValueError, IndexError):
        return False
    return bool(first & 0x02)


# ---------- Vendor lookup (IEEE OUI database, bundled with arp-scan) ----------

_OUI_VENDOR_FILE = Path("/usr/share/arp-scan/ieee-oui.txt")

def _load_oui_vendors() -> dict[str, str]:
    """Parse arp-scan's IEEE OUI table → {oui_no_separators_lowercase: vendor}."""
    out: dict[str, str] = {}
    try:
        with _OUI_VENDOR_FILE.open() as f:
            for line in f:
                if not line or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t", 1)
                if len(parts) != 2:
                    continue
                prefix = parts[0].strip().lower()
                # 24-bit OUIs only — longer assignments (MA-M/MA-S) are rare on LAN gear.
                if len(prefix) == 6 and all(c in "0123456789abcdef" for c in prefix):
                    out[prefix] = parts[1].strip()
    except OSError as exc:
        print(f"[oui] could not load {_OUI_VENDOR_FILE}: {exc}")
    return out


_OUI_VENDOR = _load_oui_vendors()
print(f"[oui] loaded {len(_OUI_VENDOR)} vendor entries")

# Short labels for the most common vendors. The IEEE strings are long and
# contain corporate suffixes ("Inc.", "Co.,Ltd.") that read poorly in a
# hostname. For unknown vendors we fall back to the first cleaned word.
_VENDOR_ALIAS = {
    "intel corporate": "Intel",
    "intel(r) corporate": "Intel",
    "espressif inc.": "Espressif",
    "giga-byte technology co.,ltd.": "Gigabyte",
    "asustek computer inc.": "ASUS",
    "asustekcomputerinc.": "ASUS",
    "micro-star international co., ltd.": "MSI",
    "micro-star intl co., ltd.": "MSI",
    "hewlett packard": "HP",
    "hewlett-packard company": "HP",
    "hewlett packard enterprise": "HPE",
    "hp inc.": "HP",
    "dell inc.": "Dell",
    "dell": "Dell",
    "lenovo": "Lenovo",
    "lenovo mobile communication technology ltd.": "Lenovo",
    "hon hai precision ind. co.,ltd.": "Foxconn",
    "hon hai precision ind.co.,ltd.": "Foxconn",
    "liteon technology corporation": "Liteon",
    "lite-on technology corporation": "Liteon",
    "lite-on technology corp.": "Liteon",
    "realtek semiconductor corp.": "Realtek",
    "apple, inc.": "Apple",
    "apple inc": "Apple",
    "amazon technologies inc.": "Amazon",
    "cisco systems, inc": "Cisco",
    "cisco systems, inc.": "Cisco",
    "cisco-linksys, llc": "Linksys",
    "ubiquiti networks inc.": "Ubiquiti",
    "ubiquiti inc": "Ubiquiti",
    "tp-link technologies co.,ltd.": "TP-Link",
    "tp-link corporation limited": "TP-Link",
    "d-link corporation": "D-Link",
    "netgear": "Netgear",
    "netgear inc.": "Netgear",
    "mikrotik": "MikroTik",
    "mikrotikls sia": "MikroTik",
    "hikvision digital technology co.,ltd.": "Hikvision",
    "zhejiang dahua technology co.,ltd.": "Dahua",
    "axis communications ab": "Axis",
    "brother industries, ltd.": "Brother",
    "canon inc.": "Canon",
    "seiko epson corporation": "Epson",
    "kyocera corporation": "Kyocera",
    "ricoh company, ltd.": "Ricoh",
    "lexmark international inc.": "Lexmark",
    "xerox corporation": "Xerox",
    "fuji xerox co.,ltd": "Xerox",
    "zebra technologies inc.": "Zebra",
    "polycom inc.": "Polycom",
    "yealink network technology co.,ltd.": "Yealink",
    "grandstream networks inc.": "Grandstream",
    "sonos, inc.": "Sonos",
    "roku, inc.": "Roku",
    "google, inc.": "Google",
    "nest labs inc.": "Nest",
    "supermicro computer, inc.": "Supermicro",
    "asrock incorporation": "ASRock",
    "advanced micro devices, inc.": "AMD",
    "tuya smart inc.": "Tuya",
    "xiaomi communications co ltd": "Xiaomi",
    "philips lighting bv": "Philips",
    "signify b.v.": "Hue",
    "aruba networks": "Aruba",
}

_VENDOR_NAME_RE = re.compile(r"[^A-Za-z0-9]")

def _shorten_vendor(vendor: str) -> str:
    """Collapse an IEEE vendor string into a short label."""
    if not vendor:
        return ""
    alias = _VENDOR_ALIAS.get(vendor.lower().strip())
    if alias:
        return alias
    # Generic fallback: first token, alphanumerics only, capitalised.
    first = vendor.strip().split(",")[0].strip().split()[0] if vendor.strip() else ""
    first = _VENDOR_NAME_RE.sub("", first)
    return first[:20] if first else ""


def vendor_label(mac: Optional[str]) -> str:
    """Short vendor label for the MAC, or '' if unknown/randomized."""
    if not mac:
        return ""
    oui = _oui(mac)
    if len(oui) != 6 or _is_locally_administered(oui):
        return ""
    full = _OUI_VENDOR.get(oui)
    return _shorten_vendor(full) if full else ""


def suggested_name(mac: Optional[str], dhcp_names: dict,
                   wsd_names: Optional[dict] = None, ip: Optional[str] = None) -> str:
    """Best-effort default name for a freshly discovered device.

    Priority: DHCP/NetBIOS sniffer hostname (by MAC) → WS-Discovery name
    (by IP) → vendor-tagged tail → Unknown-XXXX.
    """
    if not mac:
        return "Unknown"
    try:
        mac_n = normalize_mac(mac)
    except ValueError:
        return f"Unknown-{(mac or '').replace(':','')[-4:] or '????'}"
    if mac_n in dhcp_names:
        return dhcp_names[mac_n]
    if wsd_names and ip and ip in wsd_names:
        return wsd_names[ip]
    tail = mac_n.replace(":", "")[-4:]
    label = vendor_label(mac_n)
    return f"{label}-{tail}" if label else f"Unknown-{tail}"


def classify_mac(mac: Optional[str]) -> str:
    """Returns 'pc', 'nonpc', or 'unknown'.

    'unknown' covers randomized/locally-administered MACs and OUIs not in
    either list — the UI shows these and lets the user decide.
    """
    if not mac:
        return "unknown"
    oui = _oui(mac)
    if len(oui) != 6:
        return "unknown"
    if _is_locally_administered(oui):
        return "unknown"
    if oui in _NONPC_OUIS:
        return "nonpc"
    if oui in _PC_OUIS:
        return "pc"
    return "unknown"


def send_wol_packet(mac: str) -> None:
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, ("255.255.255.255", 9))
        s.sendto(packet, ("255.255.255.255", 7))


# ---------- Storage health ----------

def check_storage() -> dict:
    """Verify the backup destination is mounted and writable; auto-remount if disconnected."""
    if not os.path.ismount(BACKUP_STORAGE):
        # Drive may have been reconnected — mount underlying device then bind.
        try:
            subprocess.run(["sudo", "/usr/local/bin/recovery-remount"],
                           capture_output=True, timeout=15, check=True)
        except Exception:
            pass
    if not os.path.ismount(BACKUP_STORAGE):
        return {"ok": False, "path": BACKUP_STORAGE,
                "error": "backup drive not connected — reconnect it and wait a moment"}
    try:
        st = os.statvfs(BACKUP_STORAGE)
    except OSError as e:
        return {"ok": False, "path": BACKUP_STORAGE, "error": f"statvfs failed: {e}"}
    free_bytes = st.f_bavail * st.f_frsize
    total_bytes = st.f_blocks * st.f_frsize
    return {
        "ok": True,
        "path": BACKUP_STORAGE,
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "free_gb": round(free_bytes / 1024**3, 1),
        "total_gb": round(total_bytes / 1024**3, 1),
    }


# ---------- API ----------

@app.get("/api/status")
async def api_status():
    hosts = load_hosts()
    ping_r, hn_r, arp_r = await asyncio.gather(
        asyncio.gather(*(ping_host(h["host"]) for h in hosts), return_exceptions=True),
        asyncio.gather(*(resolve_hostname(h["host"]) for h in hosts), return_exceptions=True),
        asyncio.gather(*(arp_lookup(h["host"]) for h in hosts), return_exceptions=True),
    )
    state = load_state()
    state, _ = prune_expired(state)
    save_state(state)
    out = []
    for h, p, hn, arp in zip(hosts, ping_r, hn_r, arp_r):
        online, latency = (False, None)
        if not isinstance(p, Exception) and p is not None:
            online, latency = p
        hostname = h.get("hostname") or (hn if not isinstance(hn, Exception) else None)
        mac = h.get("mac") or (arp if not isinstance(arp, Exception) else None)
        try:
            normalized = normalize_mac(mac) if mac else None
        except ValueError:
            normalized = None
        armed = state["armed"].get(normalized) if normalized else None
        out.append({
            "name": h.get("name") or h["host"],
            "host": h["host"],
            "hostname": hostname,
            "mac": normalized,
            "mac_source": "yaml" if h.get("mac") else ("arp" if mac else None),
            "online": online,
            "latency_ms": latency,
            "armed": armed,
            "category": classify_mac(normalized),
            "last_backup_at": last_backup_at(normalized),
            "progress": get_progress(normalized),
        })
    return {"checked_at": time.time(), "now": time.time(), "hosts": out,
            "storage": check_storage()}


_IMG_RE = re.compile(r"^img-([0-9a-f]{2}(?:-[0-9a-f]{2}){5})(?:-(\d{8}-\d{4}))?$")


# Live progress reports pushed by the recovery env's ocs-*.sh wrapper.
# Cleared after PROGRESS_TTL seconds of inactivity so stale "running"
# entries don't survive a hung script.
_progress: dict[str, dict] = {}  # mac -> {phase, percent, elapsed, eta, rate, status, rc, updated_at}


class ProgressUpdate(BaseModel):
    phase: Optional[str] = None         # "backup" | "restore" | "completed" | "failed"
    percent: Optional[float] = None     # 0..100
    elapsed: Optional[str] = None       # "HH:MM:SS"
    eta: Optional[str] = None           # "HH:MM:SS"
    rate: Optional[str] = None          # e.g. "1.23GB/min"
    status: Optional[str] = None        # "started" | "running" | "completed" | "failed"
    rc: Optional[int] = None            # exit code on failure


@app.post("/api/progress/{mac}")
async def post_progress(mac: str, payload: ProgressUpdate):
    try:
        mac_n = normalize_mac(mac)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    entry = _progress.get(mac_n, {}).copy()
    for k, v in payload.dict(exclude_none=True).items():
        entry[k] = v
    entry["updated_at"] = time.time()
    _progress[mac_n] = entry
    return {"ok": True}


@app.delete("/api/progress/{mac}")
async def delete_progress(mac: str):
    try:
        mac_n = normalize_mac(mac)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _progress.pop(mac_n, None)
    return {"ok": True}


def get_progress(mac: Optional[str]) -> Optional[dict]:
    if not mac:
        return None
    entry = _progress.get(mac)
    if not entry:
        return None
    if time.time() - entry.get("updated_at", 0) > PROGRESS_TTL:
        return None
    return entry


IN_PROGRESS_WINDOW = 30  # seconds since last write -> considered in-progress


@app.get("/api/images")
async def api_images():
    """List Clonezilla image directories. Sorted newest-first.

    Recognizes both `img-<MAC>` (legacy) and `img-<MAC>-<YYYYMMDD-HHMM>`.
    For in-progress images, computes a rough percent based on the size of the
    most recent completed backup for the same MAC.
    """
    base = Path(BACKUP_STORAGE)
    if not base.is_dir():
        return {"images": []}
    hosts_by_mac = {normalize_mac(h["mac"]): h for h in load_hosts() if h.get("mac")}
    now = time.time()
    images = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        try:
            size = 0
            latest_mtime = entry.stat().st_mtime
            for f in entry.rglob("*"):
                if f.is_file():
                    try:
                        fs = f.stat()
                        size += fs.st_size
                        if fs.st_mtime > latest_mtime:
                            latest_mtime = fs.st_mtime
                    except OSError:
                        continue
            m = _IMG_RE.match(entry.name)
            mac = m.group(1).replace("-", ":") if m else None
            ts = m.group(2) if m else None
            host = hosts_by_mac.get(mac) if mac else None
            images.append({
                "name": entry.name,
                "size_bytes": size,
                "mtime": latest_mtime,
                "mac": mac,
                "timestamp": ts,
                "host_name": host.get("name") if host else None,
                "host_ip": host.get("host") if host else None,
                "in_progress": (now - latest_mtime) < IN_PROGRESS_WINDOW,
                "reference_size": None,
                "estimated_percent": None,
            })
        except OSError:
            continue

    # Assign reference sizes / percents to in-progress images using the most recent
    # completed (not in-progress) backup of the same MAC.
    by_mac = {}
    for img in images:
        if img["mac"]:
            by_mac.setdefault(img["mac"], []).append(img)
    for img in images:
        if not img["in_progress"] or not img["mac"]:
            continue
        completed = [s for s in by_mac.get(img["mac"], [])
                     if s["name"] != img["name"] and not s["in_progress"]]
        if completed:
            ref = max(completed, key=lambda s: s["mtime"])["size_bytes"]
            if ref > 0:
                img["reference_size"] = ref
                img["estimated_percent"] = min(99.0, img["size_bytes"] / ref * 100)

    images.sort(key=lambda i: i["mtime"], reverse=True)
    return {"images": images}


RMIMAGE_HELPER = "/usr/local/bin/recovery-rmimage"

@app.delete("/api/images/{name}")
async def delete_image(name: str):
    if not name or not all(c.isalnum() or c in "._-" for c in name) or name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid image name")
    try:
        subprocess.run(["sudo", "-n", RMIMAGE_HELPER, name],
                       check=True, capture_output=True, text=True, timeout=60)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=e.stderr.strip() or "rmimage failed")
    return {"ok": True, "removed": name}


class NameUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=_SAFE_NAME_PAT)


@app.put("/api/host/{ip}/name")
async def update_name(ip: str, payload: NameUpdate):
    hosts = load_hosts()
    for h in hosts:
        if h.get("host") == ip:
            h["name"] = payload.name.strip() or h["host"]
            save_hosts(hosts)
            return {"ok": True, "name": h["name"]}
    raise HTTPException(status_code=404, detail="host not found")


class HostEntry(BaseModel):
    host: str
    mac: str
    name: Optional[str] = Field(default=None, max_length=64, pattern=_SAFE_NAME_PAT)


class HostBatchAdd(BaseModel):
    hosts: list[HostEntry]


@app.post("/api/hosts")
async def add_hosts(payload: HostBatchAdd):
    """Append selected discovered devices to the backup list.

    Each entry needs ip + mac. Name defaults to dhcp-name or Unknown-XXXX.
    Duplicates (matching ip or mac) are silently skipped.
    """
    if not payload.hosts:
        return {"ok": True, "added": [], "skipped": []}
    hosts = load_hosts()
    known_ips = {h.get("host") for h in hosts}
    known_macs = set()
    for h in hosts:
        if h.get("mac"):
            try:
                known_macs.add(normalize_mac(h["mac"]))
            except ValueError:
                pass
    dhcp_names = load_dhcp_names()
    added: list[dict] = []
    skipped: list[dict] = []
    for entry in payload.hosts:
        try:
            mac_n = normalize_mac(entry.mac)
        except ValueError:
            skipped.append({"host": entry.host, "mac": entry.mac, "reason": "invalid MAC"})
            continue
        if entry.host in known_ips or mac_n in known_macs:
            skipped.append({"host": entry.host, "mac": mac_n, "reason": "already in list"})
            continue
        name = _safe_name((entry.name or "").strip() or suggested_name(mac_n, dhcp_names)) or "Unknown"
        new = {"name": name, "host": entry.host, "mac": mac_n}
        hosts.append(new)
        added.append(new)
        known_ips.add(entry.host)
        known_macs.add(mac_n)
    if added:
        save_hosts(hosts)
    return {"ok": True, "added": added, "skipped": skipped}


@app.delete("/api/host/{ip}")
async def remove_host(ip: str):
    """Remove a host from the backup list. Disarms it first if armed."""
    hosts = load_hosts()
    target = next((h for h in hosts if h.get("host") == ip), None)
    if not target:
        raise HTTPException(status_code=404, detail="host not found")
    mac_n: Optional[str] = None
    if target.get("mac"):
        try:
            mac_n = normalize_mac(target["mac"])
        except ValueError:
            mac_n = None
    if mac_n:
        state = load_state()
        state, _ = prune_expired(state)
        if mac_n in state["armed"]:
            del state["armed"][mac_n]
            try:
                run_allowlist("remove", mac_n)
            except subprocess.CalledProcessError as exc:
                print(f"[remove_host] allowlist remove failed for {mac_n}: {exc.stderr}")
            remove_grub_armed(mac_n)
            save_state(state)
    hosts = [h for h in hosts if h.get("host") != ip]
    save_hosts(hosts)
    return {"ok": True, "removed": {"host": ip, "mac": mac_n, "name": target.get("name")}}


@app.post("/api/wake/{ip}")
async def wake(ip: str):
    hosts = load_hosts()
    target = next((h for h in hosts if h.get("host") == ip), None)
    if not target:
        raise HTTPException(status_code=404, detail="host not found")
    mac = target.get("mac") or await arp_lookup(ip)
    if not mac:
        raise HTTPException(status_code=400, detail="no MAC available (not in YAML, not in ARP table — try pinging the host first)")
    try:
        mac = normalize_mac(mac)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        send_wol_packet(mac)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"send failed: {e}")
    return {"ok": True, "mac": mac}


class ArmRequest(BaseModel):
    mode: Mode
    image: Optional[str] = None  # restore-only: override image dir name (e.g. "img-aa-bb-..."), default = host's own


def _resolve_mac_and_persist(target: dict, hosts: list[dict], arp_mac: Optional[str]) -> Optional[str]:
    """Get MAC for this host, persisting to hosts.yml if discovered via ARP."""
    mac = target.get("mac") or arp_mac
    if not mac:
        return None
    mac = normalize_mac(mac)
    if not target.get("mac"):
        target["mac"] = mac
        save_hosts(hosts)
    return mac


@app.post("/api/host/{ip}/mode")
async def arm_host(ip: str, payload: ArmRequest):
    hosts = load_hosts()
    target = next((h for h in hosts if h.get("host") == ip), None)
    if not target:
        raise HTTPException(status_code=404, detail="host not found")
    arp_mac = await arp_lookup(ip)
    try:
        mac = _resolve_mac_and_persist(target, hosts, arp_mac)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not mac:
        raise HTTPException(status_code=400, detail="no MAC available — set it in hosts.yml or ping the host first")

    state = load_state()
    state, _ = prune_expired(state)
    save_state(state)  # persist any pruning side-effects

    expires_at = time.time() + ARM_TTL

    # 1. Write per-MAC grub.cfg first (idempotent — overwrites if exists)
    try:
        write_grub_armed(mac, payload.mode, payload.image)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"grub config write failed: {e.stderr.strip() or e}")

    # 2. Add to dnsmasq allowlist
    try:
        run_allowlist("add", mac)
    except subprocess.CalledProcessError as e:
        # Roll back step 1.
        remove_grub_armed(mac)
        raise HTTPException(status_code=500, detail=f"allowlist add failed: {e.stderr.strip() or e}")

    # 3. Persist state last
    state["armed"][mac] = {
        "mode": payload.mode,
        "armed_at": time.time(),
        "expires_at": expires_at,
        "host_ip": ip,
    }
    try:
        save_state(state)
    except Exception as e:
        # State save failure: roll back the real-world side effects so we don't
        # leave the host armed-without-record.
        try:
            run_allowlist("remove", mac)
        except Exception:
            pass
        remove_grub_armed(mac)
        raise HTTPException(status_code=500, detail=f"state save failed: {e}")

    print(f"[arm] {ip} ({mac}) -> {payload.mode} (image={payload.image or 'own'}), expires {expires_at}")
    return {"ok": True, "mac": mac, "mode": payload.mode, "image": payload.image, "expires_at": expires_at}


@app.delete("/api/host/{ip}/mode")
async def disarm_host(ip: str):
    state = load_state()
    state, _ = prune_expired(state)
    macs = [m for m, e in state["armed"].items() if e.get("host_ip") == ip]
    for m in macs:
        del state["armed"][m]
        try:
            run_allowlist("remove", m)
        except subprocess.CalledProcessError as e:
            print(f"[disarm] allowlist remove failed for {m}: {e.stderr}")
        remove_grub_armed(m)
    save_state(state)
    print(f"[disarm] {ip} -> cleared {macs}")
    return {"ok": True, "disarmed_macs": macs}


@app.delete("/api/arm/{mac}")
async def disarm_mac(mac: str):
    """Disarm by MAC; called from inside the live recovery env to break the reboot loop."""
    try:
        mac_n = normalize_mac(mac)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    state = load_state()
    state, _ = prune_expired(state)
    cleared = False
    if mac_n in state["armed"]:
        del state["armed"][mac_n]
        cleared = True
        try:
            run_allowlist("remove", mac_n)
        except subprocess.CalledProcessError as e:
            print(f"[disarm-mac] allowlist remove failed for {mac_n}: {e.stderr}")
    remove_grub_armed(mac_n)
    save_state(state)
    print(f"[disarm-mac] {mac_n} cleared={cleared}")
    return {"ok": True, "mac": mac_n, "cleared": cleared}


# ---------- Network scan ----------

async def get_gateway() -> Optional[str]:
    proc = await asyncio.create_subprocess_exec(
        "ip", "route", "show", "default",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    parts = out.decode(errors="ignore").split()
    if "via" in parts:
        return parts[parts.index("via") + 1]
    return None


async def get_own_ips() -> set[str]:
    proc = await asyncio.create_subprocess_exec(
        "ip", "-4", "-o", "addr", "show",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    ips: set[str] = set()
    for line in out.decode(errors="ignore").splitlines():
        parts = line.split()
        if "inet" in parts:
            ips.add(parts[parts.index("inet") + 1].split("/", 1)[0])
    return ips


# ---------- WS-Discovery (active Windows hostname probe) ----------

WSD_GROUP = "239.255.255.250"
WSD_PORT = 3702
WSD_PROBE_TIMEOUT = 3.0   # seconds to collect ProbeMatch responses
WSD_GET_TIMEOUT = 2.0     # per-host metadata GET timeout

_WSD_PROBE_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
    ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
    '<s:Header>'
    '<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>'
    '<a:MessageID>urn:uuid:{mid}</a:MessageID>'
    '<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>'
    '</s:Header><s:Body><d:Probe/></s:Body></s:Envelope>'
)

_WSD_GET_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing">'
    '<s:Header>'
    '<a:To>{epr}</a:To>'
    '<a:Action s:mustUnderstand="1">http://schemas.xmlsoap.org/ws/2004/09/transfer/Get</a:Action>'
    '<a:MessageID>urn:uuid:{mid}</a:MessageID>'
    '<a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>'
    '</s:Header><s:Body/></s:Envelope>'
)

_WSD_EPR_RE = re.compile(r"<[^>]*Address[^>]*>(urn:uuid:[a-fA-F0-9-]+)</", re.IGNORECASE)
_WSD_XADDR_RE = re.compile(r"<[^>]*XAddrs[^>]*>([^<]+)</", re.IGNORECASE)
_WSD_COMPUTER_RE = re.compile(r"<[^>]*:Computer[^>]*>([^<]+)</", re.IGNORECASE)
_WSD_FRIENDLY_RE = re.compile(r"<[^>]*FriendlyName[^>]*>([^<]+)</", re.IGNORECASE)


def _wsd_probe_sync(source_ip: str) -> dict[str, str]:
    """Send a WSD Probe, collect ProbeMatches, GET metadata, return {ip: name}.

    Runs synchronously — called via run_in_executor from the async scan path.
    Failures (timeouts, parse errors) are swallowed: WSD is best-effort.
    """
    import urllib.request, urllib.error
    import uuid as _uuid

    results: dict[str, str] = {}
    responders: dict[str, tuple[str, str]] = {}  # ip -> (epr, xaddr)
    probe = _WSD_PROBE_TEMPLATE.format(mid=_uuid.uuid4())
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(source_ip))
        except OSError:
            pass
        sock.bind((source_ip, 0))
        sock.settimeout(WSD_PROBE_TIMEOUT)
        sock.sendto(probe.encode(), (WSD_GROUP, WSD_PORT))
        deadline = time.time() + WSD_PROBE_TIMEOUT
        while time.time() < deadline:
            sock.settimeout(max(0.05, deadline - time.time()))
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                break
            except OSError:
                break
            ip = addr[0]
            if ip in responders:
                continue
            text = data.decode(errors="replace")
            epr_m = _WSD_EPR_RE.search(text)
            xaddr_m = _WSD_XADDR_RE.search(text)
            if not (epr_m and xaddr_m):
                continue
            # XAddrs can be space-separated; pick the one matching the responder IP.
            xaddrs = xaddr_m.group(1).split()
            picked = next((u for u in xaddrs if f"//{ip}:" in u or f"//{ip}/" in u),
                          xaddrs[0] if xaddrs else None)
            if not picked:
                continue
            responders[ip] = (epr_m.group(1), picked)
    finally:
        sock.close()
    for ip, (epr, xaddr) in responders.items():
        body = _WSD_GET_TEMPLATE.format(epr=epr, mid=_uuid.uuid4()).encode()
        req = urllib.request.Request(
            xaddr, data=body, method="POST",
            headers={"Content-Type":
                     'application/soap+xml;charset=utf-8;action='
                     '"http://schemas.xmlsoap.org/ws/2004/09/transfer/Get"'},
        )
        try:
            with urllib.request.urlopen(req, timeout=WSD_GET_TIMEOUT) as r:
                meta = r.read(16384).decode(errors="replace")
        except (urllib.error.URLError, socket.timeout, OSError):
            continue
        # <pub:Computer>NAME/Workgroup:...</pub:Computer> is the actual NetBIOS name.
        m = _WSD_COMPUTER_RE.search(meta)
        if m:
            raw = m.group(1).strip()
            name = raw.split("/", 1)[0].strip()
            if name and name.lower() not in (
                "microsoft publication service device host",):
                results[ip] = name
                continue
        # Fall back to FriendlyName, which on printers/cameras carries the model.
        m = _WSD_FRIENDLY_RE.search(meta)
        if m:
            name = m.group(1).strip()
            # Skip the generic Microsoft host name many Windows boxes return here.
            if name and name.lower() != "microsoft publication service device host":
                results[ip] = name
    return results


async def wsd_discover_names(source_ip: str) -> dict[str, str]:
    """Async wrapper around _wsd_probe_sync; returns {} on any error."""
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _wsd_probe_sync, source_ip)
    except Exception as exc:
        print(f"[wsd] probe failed: {exc}")
        return {}


async def arp_scan_subnet() -> list[tuple[str, str]]:
    """Return [(ip, mac), ...] for live hosts on the configured interface's local subnet."""
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "/usr/sbin/arp-scan",
        f"--interface={INTERFACE}", "--localnet", "--quiet", "--plain",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="ignore").strip() or "arp-scan failed")
    results: list[tuple[str, str]] = []
    for line in out.decode(errors="ignore").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].count(".") == 3:
            try:
                results.append((parts[0], normalize_mac(parts[1])))
            except ValueError:
                continue
    return results


@app.post("/api/scan")
async def api_scan():
    try:
        found, gateway, own_ips = await asyncio.gather(
            arp_scan_subnet(), get_gateway(), get_own_ips()
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    hosts = load_hosts()
    known_macs = {normalize_mac(h["mac"]) for h in hosts if h.get("mac")}
    known_ips = {h["host"] for h in hosts}
    skip_ips = own_ips | ({gateway} if gateway else set())
    dhcp_names = load_dhcp_names()
    # WS-Discovery: active probe to ask Windows hosts for their computer name.
    # Pick the non-loopback own IP so the multicast actually leaves the box.
    source_ip = next((i for i in own_ips if not i.startswith("127.")), None)
    wsd_names = await wsd_discover_names(source_ip) if source_ip else {}

    added: list[dict] = []
    renamed: list[dict] = []
    replaced: list[dict] = []

    # Detect MAC changes: same IP, different MAC = host was swapped.
    by_ip = {h["host"]: h for h in hosts}
    state = None
    for ip, new_mac in found:
        if ip in skip_ips:
            continue
        host = by_ip.get(ip)
        if not host:
            continue
        old_mac_raw = host.get("mac")
        if not old_mac_raw:
            host["mac"] = new_mac
            known_macs.add(new_mac)
            continue
        try:
            old_mac_n = normalize_mac(old_mac_raw)
        except ValueError:
            old_mac_n = None
        if old_mac_n == new_mac:
            continue
        new_name = _safe_name(dhcp_names.get(new_mac)) or f"Unknown-{new_mac.replace(':', '')[-4:]}"
        replaced.append({
            "host": ip,
            "old_mac": old_mac_n or old_mac_raw,
            "new_mac": new_mac,
            "old_name": host.get("name"),
            "new_name": new_name,
        })
        # Disarm the old MAC if armed.
        if old_mac_n:
            if state is None:
                state = load_state()
                state, _ = prune_expired(state)
            if old_mac_n in state["armed"]:
                del state["armed"][old_mac_n]
                try:
                    run_allowlist("remove", old_mac_n)
                except subprocess.CalledProcessError as exc:
                    print(f"[scan-replace] allowlist remove failed for {old_mac_n}: {exc.stderr}")
                remove_grub_armed(old_mac_n)
            known_macs.discard(old_mac_n)
        host["mac"] = new_mac
        host["name"] = new_name
        known_macs.add(new_mac)
    if state is not None:
        save_state(state)

    # Build {mac: name} from WSD by matching current arp-scan IPs.
    wsd_by_mac: dict[str, str] = {}
    if wsd_names:
        for ip_, mac_ in found:
            if ip_ in wsd_names:
                wsd_by_mac[mac_] = wsd_names[ip_]

    # Upgrade auto-named hosts (Unknown-XXXX / Vendor-XXXX) when DHCP or WSD
    # now knows the real hostname. DHCP cache wins over WSD if both have it.
    for h in hosts:
        mac = h.get("mac")
        if not mac or not is_auto_name(h.get("name", "")):
            continue
        try:
            mac_n = normalize_mac(mac)
        except ValueError:
            continue
        real = _safe_name(dhcp_names.get(mac_n) or wsd_by_mac.get(mac_n))
        if real and real != h["name"]:
            renamed.append({"host": h["host"], "from": h["name"], "to": real})
            h["name"] = real

    discovered: list[dict] = []
    for ip, mac in found:
        if ip in skip_ips or ip in known_ips or mac in known_macs:
            continue
        discovered.append({
            "name": _safe_name(suggested_name(mac, dhcp_names, wsd_names, ip)) or "Unknown",
            "host": ip,
            "mac": mac,
            "category": classify_mac(mac),
            "vendor": vendor_label(mac),
        })

    # Deduplicate by MAC: same physical NIC, multiple stale IP entries.
    # Priority: manual name beats auto-name; then online beats offline.
    # Manual intent outranks current network state because PXE transients
    # briefly assign machines to other IPs from the router's lease pool.
    removed: list[dict] = []
    online_ips = {ip for ip, _ in found}
    by_mac: dict[str, list[dict]] = {}
    for h in hosts:
        m = h.get("mac")
        if not m:
            continue
        try:
            by_mac.setdefault(normalize_mac(m), []).append(h)
        except ValueError:
            continue
    drop_ids: set[int] = set()
    for mac_n, entries in by_mac.items():
        if len(entries) < 2:
            continue
        named = [h for h in entries if not is_auto_name(h.get("name", ""))]
        winner = None
        reason = None
        if len(named) == 1:
            winner, reason = named[0], "duplicate MAC — other entry has a manual name"
        elif len(named) == 0:
            live = [h for h in entries if h["host"] in online_ips]
            if len(live) == 1:
                winner, reason = live[0], "duplicate MAC — other entry is currently online"
        # If multiple have manual names, or none online and none manually named,
        # the situation is ambiguous — leave both entries alone.
        if not winner:
            continue
        for h in entries:
            if h is winner:
                continue
            removed.append({"host": h["host"], "mac": mac_n,
                            "name": h.get("name"), "kept_host": winner["host"],
                            "reason": reason})
            drop_ids.add(id(h))
    if drop_ids:
        hosts = [h for h in hosts if id(h) not in drop_ids]

    if renamed or replaced or removed:
        save_hosts(hosts)
    return {"ok": True, "added": added, "renamed": renamed,
            "replaced": replaced, "removed": removed,
            "discovered": discovered, "scanned": len(found)}


# ---------- iPXE endpoint (called by booting clients) ----------

@app.get("/ipxe/boot", response_class=PlainTextResponse)
async def ipxe_boot(mac: str = ""):
    try:
        mac_n = normalize_mac(mac)
    except ValueError:
        return PlainTextResponse(IPXE_LOCAL, media_type="text/plain")

    state = load_state()
    state, _ = prune_expired(state)
    entry = state["armed"].get(mac_n)
    if not entry:
        save_state(state)
        print(f"[ipxe] {mac_n}: not armed -> boot local")
        return PlainTextResponse(IPXE_LOCAL, media_type="text/plain")

    mode = entry["mode"]
    # Consume: remove from state and allowlist (one-shot)
    del state["armed"][mac_n]
    save_state(state)
    try:
        run_allowlist("remove", mac_n)
    except subprocess.CalledProcessError as e:
        print(f"[ipxe] allowlist remove failed for {mac_n}: {e.stderr}")

    ipxe_file = IPXE_DIR / MODE_TO_IPXE_FILE[mode]
    try:
        script = ipxe_file.read_text()
    except OSError as e:
        print(f"[ipxe] failed to read {ipxe_file}: {e}")
        return PlainTextResponse(IPXE_LOCAL, media_type="text/plain")
    print(f"[ipxe] {mac_n}: served {mode} script, consumed")
    return PlainTextResponse(script, media_type="text/plain")


# ---------- Page ----------

@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Recovery Status</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; background:#111; color:#ddd; margin:0; padding:24px; }
  h1 { margin:0 0 4px; font-size:22px; }
  .brand { color:#666; font-size:11px; text-transform:uppercase; letter-spacing:0.15em; margin-bottom:4px; }
  .sub { color:#888; font-size:13px; margin-bottom:20px; }
  table { border-collapse: collapse; width:100%; max-width:1200px; }
  th, td { text-align:left; padding:10px 12px; border-bottom:1px solid #2a2a2a; vertical-align:middle; }
  th { color:#888; font-weight:500; font-size:12px; text-transform:uppercase; letter-spacing:0.05em; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:8px; vertical-align:middle; }
  .online  .dot { background:#3fb950; box-shadow:0 0 6px #3fb950; }
  .offline .dot { background:#f85149; }
  .latency, .mac, .hostname { color:#bbb; font-variant-numeric: tabular-nums; font-family: ui-monospace, monospace; font-size:13px; }
  .muted { color:#666; }
  .name-edit { background:transparent; color:#ddd; border:1px solid transparent; padding:4px 6px; border-radius:4px; font:inherit; width:100%; min-width:120px; }
  .name-edit:hover { border-color:#333; }
  .name-edit:focus { outline:none; border-color:#3b82f6; background:#1a1a1a; }
  .actions { white-space:nowrap; display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
  button { font:inherit; font-size:12px; padding:6px 10px; border-radius:5px; border:1px solid transparent; cursor:pointer; }
  button:disabled { opacity:0.4; cursor:not-allowed; }
  button.wol      { background:#1f6feb; color:#fff; }
  button.wol:hover:not(:disabled){ background:#2f7fff; }
  button.recovery { background:#1f6feb; color:#fff; }
  button.recovery:hover:not(:disabled){ background:#2f7fff; }
  button.backup   { background:#8a5a00; color:#fff; }
  button.backup:hover:not(:disabled){ background:#a86d00; }
  button.disarm   { background:#3a3a3a; color:#ddd; border-color:#555; }
  button.disarm:hover:not(:disabled){ background:#4a4a4a; }
  button.scan     { background:#2a2a2a; color:#ddd; border-color:#444; }
  button.scan:hover:not(:disabled){ background:#3a3a3a; }
  .header { display:flex; align-items:center; justify-content:space-between; max-width:1200px; margin-bottom:20px; gap:16px; }
  .header h1 { margin:0 0 4px; }
  .header .sub { margin:0; }
  .armed { display:inline-flex; align-items:center; gap:8px; padding:4px 10px; border-radius:12px; font-size:12px; font-weight:500; }
  .armed.recovery { background:rgba(31,111,235,0.15); color:#79b8ff; border:1px solid rgba(31,111,235,0.4); }
  .armed.backup   { background:rgba(138,90,0,0.2);    color:#f0b85a; border:1px solid rgba(138,90,0,0.5); }
  .armed .ttl { color:#aaa; font-variant-numeric: tabular-nums; }
  .banner { max-width:1200px; padding:10px 14px; border-radius:6px; margin-bottom:16px; font-size:13px; display:none; }
  .banner.err { display:block; background:rgba(248,81,73,0.12); border:1px solid #f85149; color:#ffb4af; }
  .banner.warn { display:block; background:rgba(210,153,34,0.12); border:1px solid #d29922; color:#f0c674; line-height:1.6; }
  .banner.warn .banner-dismiss { background:transparent; border:1px solid #d29922; color:#f0c674; margin-left:8px; padding:2px 8px; font-size:11px; }
  .banner.warn .banner-dismiss:hover { background:rgba(210,153,34,0.2); }
  .banner.ok-info { display:block; background:rgba(63,185,80,0.08); border:1px solid #2a2a2a; color:#9aa; font-size:12px; }
  #toast { position:fixed; bottom:24px; right:24px; background:#1a1a1a; border:1px solid #333; padding:12px 16px; border-radius:6px; font-size:13px; opacity:0; transition:opacity .2s; max-width:400px; }
  #toast.show { opacity:1; }
  #toast.err { border-color:#f85149; }
  #toast.ok  { border-color:#3fb950; }
  .modal-bg { position:fixed; inset:0; background:rgba(0,0,0,0.6); display:none; align-items:center; justify-content:center; z-index:10; }
  .modal-bg.show { display:flex; }
  .modal { background:#161616; border:1px solid #333; border-radius:8px; padding:20px; min-width:480px; max-width:90vw; max-height:80vh; overflow:auto; }
  .modal h2 { margin:0 0 4px; font-size:16px; }
  .modal .sub { color:#888; font-size:12px; margin-bottom:14px; }
  .modal ul.imgs { list-style:none; padding:0; margin:0 0 16px; max-height:50vh; overflow:auto; }
  .modal ul.imgs li { padding:10px 12px; border:1px solid #2a2a2a; border-radius:5px; margin-bottom:6px; cursor:pointer; display:flex; justify-content:space-between; gap:12px; align-items:center; }
  .modal ul.imgs li:hover { background:#1d1d1d; }
  .modal ul.imgs li.selected { border-color:#1f6feb; background:rgba(31,111,235,0.08); }
  .backups { margin-top:24px; }
  .backups h2 { font-size:13px; color:#888; font-weight:500; text-transform:uppercase; letter-spacing:0.05em; margin:0 0 8px; }
  .backups select.action { background:#222; color:#ddd; border:1px solid #333; border-radius:4px; padding:4px 8px; font-size:12px; cursor:pointer; }
  .backups select.action:focus { outline:none; border-color:#1f6feb; }
  .progress { display:flex; flex-direction:column; gap:3px; min-width:180px; }
  .progress .bar { position:relative; height:12px; background:#222; border:1px solid #333; border-radius:3px; overflow:hidden; }
  .progress .fill { height:100%; background:linear-gradient(90deg,#1f6feb,#3b82f6); transition:width 1s ease; }
  .progress .label { font-size:11px; color:#aaa; font-variant-numeric: tabular-nums; }
  .progress.indeterminate .fill { width:30% !important; animation: indet 1.4s ease-in-out infinite; }
  @keyframes indet { 0%{ transform:translateX(-100%);} 100%{ transform:translateX(330%);} }
  .modal ul.imgs .name { font-family: ui-monospace, monospace; font-size:13px; color:#ddd; }
  .modal ul.imgs .meta { font-size:11px; color:#888; font-variant-numeric: tabular-nums; }
  .modal ul.imgs .badge { font-size:10px; background:rgba(63,185,80,0.15); color:#3fb950; padding:2px 6px; border-radius:3px; text-transform:uppercase; letter-spacing:0.05em; }
  .modal .row { display:flex; justify-content:flex-end; gap:8px; }
  .modal .row button { padding:8px 14px; border-radius:5px; cursor:pointer; font-size:13px; border:1px solid #333; background:#222; color:#ddd; }
  .modal .row button.primary { background:#1f6feb; border-color:#1f6feb; color:#fff; }
  .modal .row button:disabled { opacity:0.5; cursor:not-allowed; }
  .cat-badge { display:inline-block; font-size:10px; padding:1px 6px; border-radius:3px; margin-left:6px; text-transform:uppercase; letter-spacing:0.05em; vertical-align:middle; }
  .cat-badge.pc    { background:rgba(63,185,80,0.12);  color:#3fb950; border:1px solid rgba(63,185,80,0.35); }
  .cat-badge.nonpc { background:rgba(248,81,73,0.10);  color:#f0857c; border:1px solid rgba(248,81,73,0.35); }
  .cat-badge.unknown { background:#222; color:#888; border:1px solid #333; }
  button.remove-btn { background:transparent; color:#888; border:1px solid #333; padding:4px 9px; }
  button.remove-btn:hover { background:rgba(248,81,73,0.15); border-color:#f85149; color:#ff9c95; }
  .done-badge { display:inline-flex; align-items:center; padding:4px 9px; border-radius:5px; font-size:12px; font-weight:500; }
  .done-badge.ok  { background:rgba(63,185,80,0.12); color:#3fb950; border:1px solid rgba(63,185,80,0.35); }
  .done-badge.err { background:rgba(248,81,73,0.12); color:#f0857c; border:1px solid rgba(248,81,73,0.40); }
  button.done-dismiss { background:transparent; color:#888; border:1px solid #333; padding:3px 7px; font-size:11px; margin-left:-3px; border-left:none; border-top-left-radius:0; border-bottom-left-radius:0; }
  button.done-dismiss:hover { background:#222; color:#ddd; }
  .addDevices-toolbar { display:flex; gap:8px; margin-bottom:8px; }
  .addDevices-toolbar .link { background:transparent; border:none; color:#79b8ff; font-size:12px; padding:2px 4px; cursor:pointer; }
  .addDevices-toolbar .link:hover { text-decoration:underline; }
  .addDevices-row { display:flex; align-items:center; gap:10px; padding:8px 10px; border:1px solid #2a2a2a; border-radius:5px; margin-bottom:5px; cursor:pointer; }
  .addDevices-row:hover { background:#1d1d1d; }
  .addDevices-row.checked { border-color:#1f6feb; background:rgba(31,111,235,0.08); }
  .addDevices-row input[type=checkbox] { accent-color:#1f6feb; }
  .addDevices-row .info { flex:1; min-width:0; }
  .addDevices-row .info .name { font-size:13px; color:#ddd; }
  .addDevices-row .info .meta { font-size:11px; color:#888; font-family: ui-monospace, monospace; }
  .addDevices-row .info .name input { background:transparent; color:#ddd; border:1px solid transparent; padding:2px 4px; border-radius:3px; font:inherit; width:100%; max-width:260px; }
  .addDevices-row .info .name input:hover { border-color:#333; }
  .addDevices-row .info .name input:focus { outline:none; border-color:#3b82f6; background:#1a1a1a; }
</style>
</head>
<body>
  <div class="header">
    <div>
      <div class="brand">FTD Aero Recovery Center</div>
      <h1>Recovery Status</h1>
      <div class="sub" id="meta">Loading…</div>
    </div>
    <button class="scan" id="addDevicesBtn">+ Add backup devices</button>
  </div>
  <div class="banner" id="storageBanner"></div>
  <div class="banner" id="warnBanner"></div>
  <div class="modal-bg" id="restoreModal">
    <div class="modal">
      <h2>Restore which image?</h2>
      <div class="sub" id="restoreModalSub"></div>
      <ul class="imgs" id="restoreList"></ul>
      <div class="row">
        <button id="restoreCancel">Cancel</button>
        <button id="restoreConfirm" class="primary" disabled>Arm restore</button>
      </div>
    </div>
  </div>
  <div class="modal-bg" id="addDevicesModal">
    <div class="modal">
      <h2>Add backup devices</h2>
      <div class="sub" id="addDevicesSub">Scanning network…</div>
      <div class="addDevices-toolbar">
        <button id="addDevicesSelectAll" class="link">Select all</button>
        <button id="addDevicesSelectPcs" class="link">Select PCs only</button>
        <button id="addDevicesSelectNone" class="link">Clear</button>
      </div>
      <ul class="imgs" id="addDevicesList"></ul>
      <div class="row">
        <button id="addDevicesCancel">Cancel</button>
        <button id="addDevicesConfirm" class="primary" disabled>Add selected</button>
      </div>
    </div>
  </div>
  <table>
    <thead><tr>
      <th>Status</th><th>Name</th><th>IP</th><th>MAC</th><th>Latency</th><th>Last Backup</th><th>Actions</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <section class="backups">
    <h2>Backups</h2>
    <table>
      <thead><tr>
        <th>Host</th><th>Image</th><th>Date</th><th>Size</th><th>Actions</th>
      </tr></thead>
      <tbody id="backupRows"></tbody>
    </table>
  </section>
  <div id="toast"></div>

<script>
let serverNow = 0;
let lastFetch = 0;

function toast(msg, kind) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show ' + (kind || '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.className = '', 3500);
}

function fmtTtl(secs) {
  if (secs <= 0) return '0:00';
  const m = Math.floor(secs / 60), s = Math.floor(secs % 60);
  return m + ':' + String(s).padStart(2, '0');
}

function fmtRelative(secs) {
  if (secs < 60) return 'just now';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
  if (secs < 86400 * 30) return Math.floor(secs / 86400) + 'd ago';
  if (secs < 86400 * 365) return Math.floor(secs / (86400 * 30)) + 'mo ago';
  return Math.floor(secs / (86400 * 365)) + 'y ago';
}

async function saveName(ip, input) {
  const newName = input.value.trim();
  if (!newName || newName === input.dataset.original) {
    input.value = input.dataset.original;
    return;
  }
  try {
    const r = await fetch(`/api/host/${encodeURIComponent(ip)}/name`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: newName})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'save failed');
    input.dataset.original = d.name;
    input.value = d.name;
    toast('Name saved', 'ok');
  } catch (e) {
    input.value = input.dataset.original;
    toast('Error: ' + e.message, 'err');
  }
}

async function wake(ip, btn) {
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '…';
  try {
    const r = await fetch(`/api/wake/${encodeURIComponent(ip)}`, { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'wake failed');
    toast(`WoL sent → ${d.mac}`, 'ok');
  } catch (e) { toast('Error: ' + e.message, 'err'); }
  finally { btn.textContent = orig; btn.disabled = false; }
}

async function shutdown(ip, btn) {
  if (!window.confirm(`Send shutdown signal to ${ip}?`)) return;
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '…';
  try {
    await fetch(`http://${ip}:8887/shutdown`, { method: 'POST', mode: 'no-cors' });
    toast(`Shutdown requested → ${ip}`, 'ok');
  } catch (e) { toast('Error: ' + e.message, 'err'); }
  finally { btn.textContent = orig; btn.disabled = false; }
}

async function arm(ip, mode, btn, image) {
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '…';
  try {
    const body = image ? {mode, image} : {mode};
    const r = await fetch(`/api/host/${encodeURIComponent(ip)}/mode`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'arm failed');
    const imgNote = image ? ` from ${image}` : '';
    toast(`${mode} armed (${d.mac})${imgNote} for 5 min`, 'ok');
    refresh();
  } catch (e) { toast('Error: ' + e.message, 'err'); }
  finally { btn.textContent = orig; btn.disabled = false; }
}

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024**2) return (n/1024).toFixed(1) + ' KB';
  if (n < 1024**3) return (n/1024**2).toFixed(1) + ' MB';
  return (n/1024**3).toFixed(1) + ' GB';
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function openRestorePicker(host, btn) {
  const modal = document.getElementById('restoreModal');
  const list = document.getElementById('restoreList');
  const sub = document.getElementById('restoreModalSub');
  const confirm = document.getElementById('restoreConfirm');
  const cancel = document.getElementById('restoreCancel');
  sub.textContent = `Target: ${host.name} (${host.host}) — MAC ${host.mac || '?'}`;
  list.innerHTML = '<li class="muted">Loading…</li>';
  modal.classList.add('show');
  confirm.disabled = true;
  let selected = null;
  try {
    const r = await fetch('/api/images', { cache: 'no-store' });
    const d = await r.json();
    if (!d.images.length) {
      list.innerHTML = '<li class="muted">No images available.</li>';
      return;
    }
    const ownMac = (host.mac || '').toLowerCase().replace(/:/g,'-');
    const ownPrefix = ownMac ? `img-${ownMac}` : null;
    const isMatching = (img) => ownPrefix && (img.name === ownPrefix || img.name.startsWith(ownPrefix + '-'));
    list.innerHTML = d.images.map(img => {
      const isOwn = isMatching(img);
      const date = new Date(img.mtime * 1000).toLocaleString();
      const hostLine = img.host_name
        ? `<div class="name">${escapeHtml(img.host_name)} <span class="meta">(${escapeHtml(img.host_ip)})</span></div>`
        : `<div class="name">${escapeHtml(img.name)}</div>`;
      const metaBits = [date, fmtBytes(img.size_bytes)];
      if (img.mac) metaBits.push(img.mac);
      if (img.host_name) metaBits.push(img.name);
      return `
        <li data-name="${img.name}" ${isOwn ? 'class="selected"' : ''}>
          <div>
            ${hostLine}${isOwn ? ' <span class="badge">this host</span>' : ''}
            <div class="meta">${metaBits.join(' · ')}</div>
          </div>
        </li>`;
    }).join('');
    list.querySelectorAll('li[data-name]').forEach(li => {
      li.addEventListener('click', () => {
        list.querySelectorAll('li').forEach(x => x.classList.remove('selected'));
        li.classList.add('selected');
        selected = li.dataset.name;
        confirm.disabled = false;
      });
    });
    // Pre-select the newest image matching this host (api_images returns newest-first)
    const ownMatch = d.images.find(isMatching);
    if (ownMatch) {
      selected = ownMatch.name;
      confirm.disabled = false;
    }
  } catch (e) {
    list.innerHTML = '<li class="muted">Error loading images: ' + e.message + '</li>';
  }
  const close = () => modal.classList.remove('show');
  cancel.onclick = close;
  confirm.onclick = () => {
    if (!selected) return;
    const hostLabel = host.name || host.host;
    if (!window.confirm(
      'Arm RESTORE for ' + hostLabel + '\\n\\n' +
      'Image: ' + selected + '\\n\\n' +
      'On its next PXE boot, this host will OVERWRITE ITS DISK with the\\n' +
      'selected image. This is irreversible.\\n\\n' +
      'Continue?'
    )) {
      return;
    }
    close();
    arm(host.host, 'recovery', btn, selected);
  };
}

async function disarm(ip, btn) {
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '…';
  try {
    const r = await fetch(`/api/host/${encodeURIComponent(ip)}/mode`, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'disarm failed');
    toast('Disarmed', 'ok');
    refresh();
  } catch (e) { toast('Error: ' + e.message, 'err'); }
  finally { btn.textContent = orig; btn.disabled = false; }
}

function buildActionsCell(h, storageOk) {
  // Only block actions while the job is actively running. Terminal states
  // (completed/failed) show a small badge alongside the normal buttons so the
  // user can immediately re-trigger or remove the host.
  const p = h.progress;
  const terminal = p && (p.status === 'completed' || p.status === 'failed');
  if (p && !terminal) {
    const pct = (p.percent != null) ? Math.max(0, Math.min(100, p.percent)) : 0;
    const phase = (p.phase || 'running').toLowerCase();
    const label = `${phase.toUpperCase()} ${pct.toFixed(1)}%`;
    const metaParts = [];
    if (p.eta)     metaParts.push('ETA ' + p.eta);
    if (p.elapsed) metaParts.push('elapsed ' + p.elapsed);
    if (p.rate)    metaParts.push(p.rate);
    return `
      <div class="progress ${phase}">
        <div class="bar"><div class="fill" style="width:${pct}%"></div></div>
        <div class="label">${label}${metaParts.length ? ' · ' + metaParts.join(' · ') : ''}</div>
      </div>`;
  }
  if (h.armed) {
    const m = h.armed.mode;
    const remaining = Math.max(0, h.armed.expires_at - serverNow);
    return `
      <div class="actions">
        <span class="armed ${m}" data-expires="${h.armed.expires_at}">
          ${m.toUpperCase()} armed
          <span class="ttl">${fmtTtl(remaining)}</span>
        </span>
        <button class="disarm" data-ip="${h.host}" data-action="disarm">Disarm</button>
      </div>`;
  }
  const macAttr = h.mac ? '' : 'disabled title="No MAC known"';
  const onAttr = h.online ? '' : 'disabled title="Host offline"';
  const backupAttr = h.mac
    ? (storageOk ? '' : 'disabled title="Backup storage offline — fix the storage banner first"')
    : 'disabled title="No MAC known"';
  let terminalBadge = '';
  if (terminal) {
    const phase = (p.phase || '').toLowerCase();
    const verb = phase === 'restore' ? 'Restore' : (phase === 'backup' ? 'Backup' : 'Job');
    if (p.status === 'completed') {
      terminalBadge = `<span class="done-badge ok" title="${verb} completed">✓ ${verb} done</span>
        <button class="done-dismiss" data-ip="${h.host}" data-mac="${h.mac || ''}" data-action="dismiss-progress" title="Dismiss">✕</button>`;
    } else {
      const rc = p.rc != null ? ` (rc=${p.rc})` : '';
      terminalBadge = `<span class="done-badge err" title="${verb} failed${rc}">✗ ${verb} failed${rc}</span>
        <button class="done-dismiss" data-ip="${h.host}" data-mac="${h.mac || ''}" data-action="dismiss-progress" title="Dismiss">✕</button>`;
    }
  }
  return `
    <div class="actions">
      ${terminalBadge}
      <button class="wol"      data-ip="${h.host}" data-action="wol"      ${macAttr}>Wake</button>
      <button class="recovery" data-ip="${h.host}" data-action="recovery" ${macAttr}>Recovery</button>
      <button class="backup"   data-ip="${h.host}" data-action="backup"   ${backupAttr}>Backup</button>
      <button class="shutdown" data-ip="${h.host}" data-action="shutdown" ${onAttr}>Shutdown</button>
      <button class="remove-btn" data-ip="${h.host}" data-action="remove" title="Remove from backup list">Remove</button>
    </div>`;
}

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  if (document.activeElement && document.activeElement.classList.contains('name-edit')) return;
  refreshing = true;
  try {
    const r = await fetch('/api/status', { cache: 'no-store' });
    const d = await r.json();
    serverNow = d.now; lastFetch = Date.now() / 1000;
    const rows = document.getElementById('rows');
    if (!d.hosts.length) {
      rows.innerHTML = '<tr><td colspan="7" class="muted">No devices in the backup list yet. Click "Add backup devices" to scan and pick.</td></tr>';
    } else {
      const storageOk = !!(d.storage && d.storage.ok);
      rows.innerHTML = d.hosts.map(h => {
        const macCell = h.mac
          ? `<span class="mac">${h.mac}</span>${h.mac_source === 'arp' ? ' <span class="muted">(arp)</span>' : ''}`
          : '<span class="muted">—</span>';
        const latencyCell = h.latency_ms != null ? h.latency_ms.toFixed(2) + ' ms' : '<span class="muted">—</span>';
        const backupCell = h.last_backup_at
          ? `<span class="latency" title="${new Date(h.last_backup_at * 1000).toLocaleString()}">${fmtRelative(serverNow - h.last_backup_at)}</span>`
          : '<span class="muted">never</span>';
        const safeName = escapeHtml(h.name);
        const catBadge = h.category === 'pc' ? '<span class="cat-badge pc" title="MAC vendor is a known PC NIC">PC</span>'
                       : h.category === 'nonpc' ? '<span class="cat-badge nonpc" title="MAC vendor is a known non-PC device">non-PC</span>'
                       : '';
        return `
          <tr class="${h.online ? 'online' : 'offline'}">
            <td><span class="dot"></span>${h.online ? 'Online' : 'Offline'}</td>
            <td><input class="name-edit" data-ip="${h.host}" data-original="${safeName}" value="${safeName}">${catBadge}</td>
            <td class="mac">${h.host}</td>
            <td>${macCell}</td>
            <td class="latency">${latencyCell}</td>
            <td>${backupCell}</td>
            <td>${buildActionsCell(h, storageOk)}</td>
          </tr>`;
      }).join('');

      rows.querySelectorAll('.name-edit').forEach(inp => {
        inp.addEventListener('blur', () => saveName(inp.dataset.ip, inp));
        inp.addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); inp.blur(); }
          if (e.key === 'Escape') { inp.value = inp.dataset.original; inp.blur(); }
        });
      });
      rows.querySelectorAll('button[data-action]').forEach(btn => {
        btn.addEventListener('click', () => {
          const a = btn.dataset.action, ip = btn.dataset.ip;
          if (a === 'wol') wake(ip, btn);
          else if (a === 'shutdown') shutdown(ip, btn);
          else if (a === 'disarm') disarm(ip, btn);
          else if (a === 'remove') removeHost(ip, btn);
          else if (a === 'dismiss-progress') dismissProgress(btn.dataset.mac, btn);
          else if (a === 'recovery') {
            const host = d.hosts.find(h => h.host === ip);
            if (host) openRestorePicker(host, btn);
          }
          else arm(ip, a, btn);
        });
      });
    }
    document.getElementById('meta').textContent =
      'Last check: ' + new Date(d.checked_at * 1000).toLocaleTimeString();
    const banner = document.getElementById('storageBanner');
    const s = d.storage || {};
    if (!s.ok) {
      banner.className = 'banner err';
      banner.textContent = `⚠ Backup drive offline — ${s.error || 'unknown error'}. Backups are paused.`;
    } else {
      banner.className = 'banner ok-info';
      banner.textContent = `Backup storage: ${s.path} · ${s.free_gb} GB free of ${s.total_gb} GB`;
    }
  } catch (e) {
    document.getElementById('meta').textContent = 'Error: ' + e.message;
  } finally {
    refreshing = false;
  }
}

// Update armed countdowns once per second (without re-fetching).
setInterval(() => {
  const now = serverNow + (Date.now() / 1000 - lastFetch);
  document.querySelectorAll('.armed').forEach(el => {
    const remaining = Math.max(0, parseFloat(el.dataset.expires) - now);
    const ttl = el.querySelector('.ttl');
    if (ttl) ttl.textContent = fmtTtl(remaining);
    if (remaining === 0) refresh();
  });
}, 1000);

async function dismissProgress(mac, btn) {
  if (!mac) return;
  btn.disabled = true;
  try {
    await fetch(`/api/progress/${encodeURIComponent(mac)}`, { method: 'DELETE' });
    refresh();
  } catch (e) { toast('Error: ' + e.message, 'err'); btn.disabled = false; }
}

async function removeHost(ip, btn) {
  if (!window.confirm(`Remove ${ip} from the backup list? (Backup images already on disk are kept.)`)) return;
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '…';
  try {
    const r = await fetch(`/api/host/${encodeURIComponent(ip)}`, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'remove failed');
    toast(`Removed ${d.removed.name || ip}`, 'ok');
    refresh();
  } catch (e) { toast('Error: ' + e.message, 'err'); btn.textContent = orig; btn.disabled = false; }
}

async function openAddDevicesPicker(btn) {
  const modal   = document.getElementById('addDevicesModal');
  const list    = document.getElementById('addDevicesList');
  const sub     = document.getElementById('addDevicesSub');
  const confirm = document.getElementById('addDevicesConfirm');
  const cancel  = document.getElementById('addDevicesCancel');
  const selAll  = document.getElementById('addDevicesSelectAll');
  const selPcs  = document.getElementById('addDevicesSelectPcs');
  const selNone = document.getElementById('addDevicesSelectNone');
  sub.textContent = 'Scanning network…';
  list.innerHTML = '<li class="muted">Please wait — running arp-scan on the subnet…</li>';
  confirm.disabled = true;
  modal.classList.add('show');
  btn.disabled = true;
  let discovered = [];
  let warnings = null;
  try {
    const r = await fetch('/api/scan', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'scan failed');
    discovered = (d.discovered || []).slice();
    // Sort: PCs first, then unknowns, then non-PCs; secondary by IP numerically.
    const rank = c => c === 'pc' ? 0 : (c === 'unknown' ? 1 : 2);
    const ipKey = ip => ip.split('.').map(n => String(n).padStart(3, '0')).join('.');
    discovered.sort((a, b) => rank(a.category) - rank(b.category) || ipKey(a.host).localeCompare(ipKey(b.host)));
    warnings = { replaced: d.replaced || [], removed: d.removed || [] };
  } catch (e) {
    list.innerHTML = `<li class="muted">Scan failed: ${escapeHtml(e.message)}</li>`;
    sub.textContent = '';
    btn.disabled = false;
    return;
  } finally { btn.disabled = false; }
  if (!discovered.length) {
    sub.textContent = '';
    list.innerHTML = '<li class="muted">No new devices discovered. Everything on the subnet is either already in the list or this host itself.</li>';
  } else {
    sub.textContent = `Found ${discovered.length} device(s) not yet in the backup list. Tick the ones you want to back up.`;
    list.innerHTML = discovered.map((dev, i) => {
      const badge = dev.category === 'pc' ? '<span class="cat-badge pc">PC</span>'
                  : dev.category === 'nonpc' ? '<span class="cat-badge nonpc">non-PC</span>'
                  : '<span class="cat-badge unknown">?</span>';
      const safeName = escapeHtml(dev.name || '');
      return `
        <li class="addDevices-row" data-i="${i}">
          <input type="checkbox" data-i="${i}">
          <div class="info">
            <div class="name">
              <input type="text" data-i="${i}" data-field="name" value="${safeName}">
              ${badge}
            </div>
            <div class="meta">${escapeHtml(dev.host)} · ${escapeHtml(dev.mac)}${dev.vendor ? ' · ' + escapeHtml(dev.vendor) : ''}</div>
          </div>
        </li>`;
    }).join('');
    const updateConfirm = () => {
      const n = list.querySelectorAll('input[type=checkbox]:checked').length;
      confirm.disabled = n === 0;
      confirm.textContent = n ? `Add ${n} selected` : 'Add selected';
    };
    list.querySelectorAll('.addDevices-row').forEach(row => {
      const cb = row.querySelector('input[type=checkbox]');
      row.addEventListener('click', e => {
        if (e.target.tagName === 'INPUT') return;  // let inputs handle themselves
        cb.checked = !cb.checked;
        row.classList.toggle('checked', cb.checked);
        updateConfirm();
      });
      cb.addEventListener('change', () => {
        row.classList.toggle('checked', cb.checked);
        updateConfirm();
      });
    });
    const setAll = (predicate) => {
      list.querySelectorAll('.addDevices-row').forEach(row => {
        const i = parseInt(row.dataset.i, 10);
        const cb = row.querySelector('input[type=checkbox]');
        cb.checked = predicate(discovered[i]);
        row.classList.toggle('checked', cb.checked);
      });
      updateConfirm();
    };
    selAll.onclick  = () => setAll(_ => true);
    selPcs.onclick  = () => setAll(d => d.category === 'pc');
    selNone.onclick = () => setAll(_ => false);
  }
  // Surface MAC-change / dedup warnings even if no devices were discovered.
  const warn = document.getElementById('warnBanner');
  if (warnings && (warnings.replaced.length || warnings.removed.length)) {
    warn.className = 'banner warn';
    const parts = [];
    if (warnings.replaced.length) {
      parts.push('<strong>⚠ MAC address changed at these IPs — hardware was swapped:</strong>');
      parts.push(warnings.replaced.map(r =>
        `&nbsp;&nbsp;${escapeHtml(r.host)}: ${escapeHtml(r.old_mac)} → ${escapeHtml(r.new_mac)} (was "${escapeHtml(r.old_name)}", now "${escapeHtml(r.new_name)}")`
      ).join('<br>'));
    }
    if (warnings.removed.length) {
      if (parts.length) parts.push('<br>');
      parts.push('<strong>Removed duplicate entries (same MAC as another host):</strong>');
      parts.push(warnings.removed.map(r =>
        `&nbsp;&nbsp;${escapeHtml(r.host)} ("${escapeHtml(r.name)}") — ${escapeHtml(r.reason)}; kept ${escapeHtml(r.kept_host)}`
      ).join('<br>'));
    }
    parts.push(' <button class="banner-dismiss" type="button">dismiss</button>');
    warn.innerHTML = parts.join('<br>');
    warn.querySelector('.banner-dismiss').addEventListener('click', () => { warn.className = 'banner'; });
  }
  const close = () => modal.classList.remove('show');
  cancel.onclick = close;
  confirm.onclick = async () => {
    const picks = [];
    list.querySelectorAll('.addDevices-row').forEach(row => {
      const cb = row.querySelector('input[type=checkbox]');
      if (!cb.checked) return;
      const i = parseInt(row.dataset.i, 10);
      const nameInput = row.querySelector('input[data-field="name"]');
      picks.push({ host: discovered[i].host, mac: discovered[i].mac, name: nameInput.value.trim() });
    });
    if (!picks.length) return;
    confirm.disabled = true; const orig = confirm.textContent; confirm.textContent = 'Adding…';
    try {
      const r = await fetch('/api/hosts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({hosts: picks})
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'add failed');
      const added = (d.added || []).length;
      const skipped = (d.skipped || []).length;
      toast(`Added ${added}${skipped ? `, skipped ${skipped}` : ''}`, 'ok');
      close();
      refresh();
    } catch (e) {
      toast('Error: ' + e.message, 'err');
      confirm.textContent = orig; confirm.disabled = false;
    }
  };
}

document.getElementById('addDevicesBtn').addEventListener('click', e => openAddDevicesPicker(e.currentTarget));

async function refreshBackups() {
  try {
    const r = await fetch('/api/images', { cache: 'no-store' });
    const d = await r.json();
    const rows = document.getElementById('backupRows');
    if (!d.images.length) {
      rows.innerHTML = '<tr><td colspan="5" class="muted">No backups yet.</td></tr>';
      return;
    }
    rows.innerHTML = d.images.map(img => {
      const hostCell = img.host_name
        ? `${escapeHtml(img.host_name)} <span class="muted">(${escapeHtml(img.host_ip)})</span>`
        : '<span class="muted">unmatched</span>';
      const date = new Date(img.mtime * 1000).toLocaleString();
      let sizeCell;
      if (img.in_progress && img.estimated_percent != null) {
        const pct = img.estimated_percent.toFixed(1);
        sizeCell = `
          <div class="progress">
            <div class="bar"><div class="fill" style="width:${pct}%"></div></div>
            <div class="label">${fmtBytes(img.size_bytes)} / ~${fmtBytes(img.reference_size)} · ${pct}%</div>
          </div>`;
      } else if (img.in_progress) {
        sizeCell = `
          <div class="progress indeterminate">
            <div class="bar"><div class="fill"></div></div>
            <div class="label">${fmtBytes(img.size_bytes)} · running (no prior backup for reference)</div>
          </div>`;
      } else {
        sizeCell = fmtBytes(img.size_bytes);
      }
      return `
        <tr>
          <td>${hostCell}</td>
          <td><span class="mac">${escapeHtml(img.name)}</span></td>
          <td>${date}</td>
          <td>${sizeCell}</td>
          <td>
            <select class="action" data-name="${escapeHtml(img.name)}">
              <option value="">Action…</option>
              <option value="delete">Delete</option>
            </select>
          </td>
        </tr>`;
    }).join('');
    rows.querySelectorAll('select.action').forEach(sel => {
      sel.addEventListener('change', async () => {
        const name = sel.dataset.name;
        const choice = sel.value;
        sel.value = '';  // reset
        if (choice === 'delete') {
          if (!window.confirm(`Permanently delete image "${name}"? This cannot be undone.`)) return;
          try {
            const r = await fetch(`/api/images/${encodeURIComponent(name)}`, { method: 'DELETE' });
            const d = await r.json();
            if (!r.ok) throw new Error(d.detail || 'delete failed');
            toast(`Deleted ${name}`, 'ok');
            refreshBackups();
            refresh();
          } catch (err) { toast('Error: ' + err.message, 'err'); }
        }
      });
    });
  } catch (e) { /* silent */ }
}

refresh();
refreshBackups();
setInterval(refresh, 5000);
setInterval(refreshBackups, 5000);
</script>
</body>
</html>
"""
