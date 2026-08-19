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
VERSION = (APP_DIR / "VERSION").read_text().strip() if (APP_DIR / "VERSION").exists() else "unknown"
HOSTS_FILE = APP_DIR / "hosts.yml"
STATE_FILE = APP_DIR / "state.json"
DHCP_NAMES_FILE = APP_DIR / "dhcp_names.json"
MACHINE_NAMES_FILE = APP_DIR / "machine_names.json"
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


def backup_summary() -> dict[str, dict]:
    """Scan the image store once: {mac: {"count": n, "latest": mtime}}.

    /api/status needs both the image count and the newest timestamp for every
    device on the page, so walk the directory once instead of per host.
    """
    base = Path(BACKUP_STORAGE)
    out: dict[str, dict] = {}
    if not base.is_dir():
        return out
    try:
        entries = list(base.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir():
            continue
        m = _IMG_RE.match(entry.name)
        if not m:
            continue
        mac = m.group(1).replace("-", ":")
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        rec = out.setdefault(mac, {"count": 0, "latest": 0.0})
        rec["count"] += 1
        if mtime > rec["latest"]:
            rec["latest"] = mtime
    return out


def last_backup_at(mac: Optional[str]) -> Optional[float]:
    """Latest mtime across /srv/clonezilla-images/img-<MAC>{,-<timestamp>}/, or None."""
    if not mac:
        return None
    rec = backup_summary().get(mac)
    return rec["latest"] if rec and rec["latest"] > 0 else None

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


# ---------- machine_names.json (backup group names, keyed by MAC) ----------
#
# A backup group is identified by its MAC, not by an entry in hosts.yml: images
# outlive the device being on the network. We store the name the device had when
# the backup was armed so the group stays labelled forever, and let the operator
# rename it. A manual rename is never overwritten by a later capture or scan.

def load_machine_names() -> dict[str, dict]:
    """Return {normalized_mac: {"name": str, "source": "manual"|"auto"}}."""
    if not MACHINE_NAMES_FILE.exists():
        return {}
    try:
        raw = json.loads(MACHINE_NAMES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for mac, val in raw.items():
        try:
            mac_n = normalize_mac(mac)
        except ValueError:
            continue
        if isinstance(val, str):  # tolerate a bare {mac: name} file
            val = {"name": val, "source": "auto"}
        if not isinstance(val, dict):
            continue
        name = _safe_name(val.get("name"))
        if not name:
            continue
        out[mac_n] = {
            "name": name,
            "source": "manual" if val.get("source") == "manual" else "auto",
        }
    return out


_names_write_failed = False


def save_machine_names(names: dict[str, dict]) -> bool:
    """Persist the name map. Never raises — a read-only rootfs must not break the
    UI, and /api/images backfills on every poll, so only log the first failure."""
    global _names_write_failed
    try:
        tmp = MACHINE_NAMES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(names, indent=2, sort_keys=True))
        tmp.replace(MACHINE_NAMES_FILE)
        _names_write_failed = False
        return True
    except OSError as e:
        if not _names_write_failed:
            print(f"[machine-names] could not persist: {e}")
            _names_write_failed = True
        return False


def remember_machine_name(mac: Optional[str], name: Optional[str],
                          source: str = "auto") -> None:
    """Record the device name for a MAC. 'auto' never overwrites a manual rename."""
    if not mac:
        return
    clean = _safe_name(name)
    if not clean:
        return
    names = load_machine_names()
    existing = names.get(mac)
    if existing:
        if existing["source"] == "manual" and source != "manual":
            return
        if existing["name"] == clean and existing["source"] == source:
            return
    names[mac] = {"name": clean, "source": source}
    save_machine_names(names)


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


def save_state_quiet(state: dict, context: str) -> bool:
    """Persist state but never raise. Read/serve paths (status page, iPXE
    boot) must keep working even if the filesystem is read-only; expiry is
    re-derived from timestamps on every request anyway."""
    try:
        save_state(state)
        return True
    except OSError as e:
        print(f"[{context}] could not persist state: {e}")
        return False


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


# ---------- Drive health ----------

_drive_health_cache: dict = {}
_DRIVE_HEALTH_TTL = 60  # seconds


def _backup_device() -> Optional[str]:
    """Return the block disk device (e.g. /dev/sda, /dev/nvme1n1) backing the backup storage."""
    try:
        bind_src = subprocess.check_output(
            ["awk", '$2 == "/srv/clonezilla-images" && $1 !~ /^#/ {print $1; exit}', "/etc/fstab"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if not bind_src:
            return None
        underlying = str(Path(bind_src).parent)
        partition = subprocess.check_output(
            ["findmnt", "-no", "SOURCE", underlying],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if not partition.startswith("/dev/"):
            return None
        pkname = subprocess.check_output(
            ["lsblk", "-no", "PKNAME", partition],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return f"/dev/{pkname}" if pkname else partition
    except Exception:
        return None


def _run_smartctl(device: str, extra_flags: list[str] | None = None) -> Optional[dict]:
    flags = extra_flags or []
    try:
        proc = subprocess.run(
            ["sudo", "/usr/sbin/smartctl", "-j", "-H", "-A", "-i"] + flags + [device],
            capture_output=True, text=True, timeout=15,
        )
        return json.loads(proc.stdout)
    except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None


def _device_transport(device: str) -> str:
    try:
        return subprocess.check_output(
            ["lsblk", "-no", "TRAN", device],
            text=True, stderr=subprocess.DEVNULL,
        ).strip().lower()
    except Exception:
        return ""


def _parse_smartctl(data: dict, device: str, transport: str) -> dict:
    passed = data.get("smart_status", {}).get("passed")
    health = "PASSED" if passed is True else ("FAILED" if passed is False else "UNKNOWN")
    capacity_bytes = data.get("user_capacity", {}).get("bytes", 0)

    result: dict = {
        "ok": True,
        "device": device,
        "transport": transport or "unknown",
        "health": health,
        "temperature_c": data.get("temperature", {}).get("current"),
        "power_on_hours": data.get("power_on_time", {}).get("hours"),
        "model": data.get("model_name") or data.get("model_family"),
        "serial": data.get("serial_number"),
        "firmware": data.get("firmware_version"),
        "capacity_gb": round(capacity_bytes / 1024**3, 1) if capacity_bytes else None,
    }

    smartctl_errors = [
        m.get("string", "")
        for m in data.get("smartctl", {}).get("messages", [])
        if m.get("severity") == "error"
    ]
    if smartctl_errors:
        result["warnings"] = smartctl_errors

    nvme_log = data.get("nvme_smart_health_information_log")
    if nvme_log:
        result["type"] = "nvme"
        result["nvme"] = {
            "available_spare_pct": nvme_log.get("available_spare"),
            "available_spare_threshold_pct": nvme_log.get("available_spare_threshold"),
            "percentage_used": nvme_log.get("percentage_used"),
            "critical_warning": nvme_log.get("critical_warning"),
            "data_read_gb": round(nvme_log.get("data_units_read", 0) * 512000 / 1024**3, 1),
            "data_written_gb": round(nvme_log.get("data_units_written", 0) * 512000 / 1024**3, 1),
        }
    else:
        attrs = {
            a["id"]: a["raw"]["value"]
            for a in data.get("ata_smart_attributes", {}).get("table", [])
            if "id" in a and "raw" in a and "value" in a["raw"]
        }
        result["type"] = "sata" if attrs else "unknown"
        result["ata"] = {
            "reallocated_sectors": attrs.get(5),
            "pending_sectors": attrs.get(197),
            "uncorrectable_sectors": attrs.get(198),
        }

    return result


def get_drive_health() -> dict:
    now = time.time()
    cached = _drive_health_cache.get("result")
    if cached and now - _drive_health_cache.get("at", 0) < _DRIVE_HEALTH_TTL:
        return cached

    device = _backup_device()
    if not device:
        result: dict = {"ok": False, "error": "could not determine backup drive device"}
        _drive_health_cache.update({"result": result, "at": now})
        return result

    transport = _device_transport(device)

    data = _run_smartctl(device)
    if data is None:
        result = {"ok": False, "device": device,
                  "error": "smartctl not installed — run: sudo apt install smartmontools"}
        _drive_health_cache.update({"result": result, "at": now})
        return result

    result = _parse_smartctl(data, device, transport)

    # USB bridge blocked passthrough — retry with SAT (works on many bridges)
    if transport == "usb" and result.get("health") == "UNKNOWN":
        sat_data = _run_smartctl(device, ["-d", "sat"])
        if sat_data:
            sat_result = _parse_smartctl(sat_data, device, transport)
            if sat_result.get("health") != "UNKNOWN" or sat_result.get("temperature_c") is not None:
                sat_result["sat_passthrough"] = True
                result = sat_result

    _drive_health_cache.update({"result": result, "at": now})
    return result


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
    state, expired = prune_expired(state)
    if expired:
        save_state_quiet(state, "status")
    backups = backup_summary()
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
        b = backups.get(normalized) if normalized else None
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
            "last_backup_at": (b["latest"] if b and b["latest"] > 0 else None),
            "image_count": (b["count"] if b else 0),
            "progress": get_progress(normalized),
        })
    return {"checked_at": time.time(), "now": time.time(), "hosts": out,
            "storage": check_storage()}


@app.get("/api/drive-health")
async def api_drive_health():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_drive_health)


UPDATE_HELPER = "/usr/local/bin/recovery-update"
UPDATE_RUN_DIR = Path("/run/ftd-recovery-update")


class UpdateStart(BaseModel):
    username: str = ""
    password: str = ""


@app.post("/api/update")
async def api_update_start(payload: UpdateStart):
    if "\n" in payload.username or "\r" in payload.username:
        raise HTTPException(status_code=400, detail="invalid username")
    # Credentials go to the root helper over stdin (never argv, never disk);
    # the helper keeps them in RAM only while the VPN authenticates.
    creds = f"{payload.username}\n{payload.password}\n"
    try:
        proc = subprocess.run(
            ["sudo", "-n", UPDATE_HELPER, "start"],
            input=creds, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="update helper timed out")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="sudo not available")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "failed to start update").strip()
        raise HTTPException(status_code=500, detail=msg[:300])
    return {"started": True}


@app.post("/api/update/abort")
async def api_update_abort():
    proc = subprocess.run(
        ["sudo", "-n", UPDATE_HELPER, "abort"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "abort failed").strip()
        raise HTTPException(status_code=500, detail=msg[:300])
    return {"aborted": True}


@app.get("/api/update/status")
async def api_update_status():
    status = {"state": "idle"}
    try:
        status = json.loads((UPDATE_RUN_DIR / "status.json").read_text())
    except (OSError, ValueError):
        pass
    log_tail = ""
    try:
        lines = (UPDATE_RUN_DIR / "update.log").read_text(errors="replace").splitlines()
        log_tail = "\n".join(lines[-150:])
    except OSError:
        pass
    return {"status": status, "log": log_tail, "version": VERSION}


@app.get("/api/version")
async def api_version():
    return {"version": VERSION}


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
    machine_names = load_machine_names()
    backfilled = False
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
            stored = machine_names.get(mac) if mac else None
            # Backfill: an image taken before names were persisted (or restored
            # from an older install) adopts the name the device carries right
            # now, so the group keeps a label once the device leaves the network.
            if mac and not stored and host and _safe_name(host.get("name")):
                stored = {"name": _safe_name(host.get("name")), "source": "auto"}
                machine_names[mac] = stored
                backfilled = True
            machine_name = (stored or {}).get("name") or (host.get("name") if host else None)
            images.append({
                "name": entry.name,
                "size_bytes": size,
                "mtime": latest_mtime,
                "mac": mac,
                "timestamp": ts,
                "version": 1,
                "version_count": 1,
                "machine_name": machine_name,
                "name_source": (stored or {}).get("source"),
                "on_network": host is not None,
                "host_name": host.get("name") if host else None,
                "host_ip": host.get("host") if host else None,
                "in_progress": (now - latest_mtime) < IN_PROGRESS_WINDOW,
                "reference_size": None,
                "estimated_percent": None,
            })
        except OSError:
            continue

    if backfilled:
        save_machine_names(machine_names)

    # Assign reference sizes / percents to in-progress images using the most recent
    # completed (not in-progress) backup of the same MAC.
    by_mac = {}
    for img in images:
        if img["mac"]:
            by_mac.setdefault(img["mac"], []).append(img)

    # Version numbers are derived, never stored: oldest image of a MAC is v1.
    for group in by_mac.values():
        for n, img in enumerate(sorted(group, key=lambda s: s["mtime"]), start=1):
            img["version"] = n
            img["version_count"] = len(group)

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


@app.put("/api/machine/{mac}/name")
async def update_machine_name(mac: str, payload: NameUpdate):
    """Rename a backup group. The name is keyed by MAC, so it follows the
    machine's images through IP changes and outlives it leaving the network."""
    try:
        mac_n = normalize_mac(mac)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    name = _safe_name(payload.name)
    if not name:
        raise HTTPException(status_code=400, detail="invalid name")
    names = load_machine_names()
    names[mac_n] = {"name": name, "source": "manual"}
    if not save_machine_names(names):
        raise HTTPException(status_code=500, detail="could not persist the name")
    # It is one machine with one name: if it is currently on the network, the
    # device row must not keep showing the old label.
    hosts = load_hosts()
    changed = False
    for h in hosts:
        if not h.get("mac"):
            continue
        try:
            if normalize_mac(h["mac"]) == mac_n and h.get("name") != name:
                h["name"] = name
                changed = True
        except ValueError:
            continue
    if changed:
        save_hosts(hosts)
    return {"ok": True, "mac": mac_n, "name": name}


@app.put("/api/host/{ip}/name")
async def update_name(ip: str, payload: NameUpdate):
    hosts = load_hosts()
    for h in hosts:
        if h.get("host") == ip:
            h["name"] = payload.name.strip() or h["host"]
            save_hosts(hosts)
            # Renaming the device renames its backup group too — same machine.
            if h.get("mac"):
                try:
                    remember_machine_name(normalize_mac(h["mac"]), h["name"], "manual")
                except ValueError:
                    pass
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

    # Capture the device's current name so the backup group stays labelled even
    # after the machine leaves the network. A manual rename wins over this.
    if payload.mode == "backup":
        remember_machine_name(mac, target.get("name"), "auto")

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

    # IP moves: a known NIC re-appearing at a new address (e.g. a machine
    # that got a DHCP lease first and was later moved to a fixed IP). Follow
    # the MAC — otherwise the entry keeps its stale IP forever and the device
    # can never be re-added, because known MACs are filtered out of
    # `discovered` below.
    moved: list[dict] = []
    found_set = set(found)
    entry_by_mac: dict[str, dict] = {}
    for h in hosts:
        m = h.get("mac")
        if not m:
            continue
        try:
            entry_by_mac.setdefault(normalize_mac(m), h)
        except ValueError:
            continue
    for ip, mac in found:
        if ip in skip_ips:
            continue
        h = entry_by_mac.get(mac)
        if not h or h["host"] == ip:
            continue
        # Ambiguous cases stay untouched: the old address still answers for
        # this MAC, or the new address belongs to another entry (dedup below
        # sorts those out).
        if (h["host"], mac) in found_set or ip in by_ip:
            continue
        moved.append({"mac": mac, "name": h.get("name"),
                      "old_host": h["host"], "new_host": ip})
        known_ips.discard(h["host"])
        known_ips.add(ip)
        h["host"] = ip

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

    if renamed or replaced or removed or moved:
        save_hosts(hosts)
    return {"ok": True, "added": added, "renamed": renamed,
            "replaced": replaced, "removed": removed, "moved": moved,
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
        save_state_quiet(state, "ipxe")
        print(f"[ipxe] {mac_n}: not armed -> boot local")
        return PlainTextResponse(IPXE_LOCAL, media_type="text/plain")

    mode = entry["mode"]
    # Consume: remove from state and allowlist (one-shot). If persisting
    # fails, still serve the boot script — the arm expires by TTL anyway.
    del state["armed"][mac_n]
    save_state_quiet(state, "ipxe")
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recovery Status</title>
<style>
  :root {
    color-scheme: dark;
    --bg:#0c0c0e; --panel:#141417; --panel-2:#1a1a1e;
    --line:#26262b; --line-soft:#1e1e22;
    --fg:#e8e8ea; --fg-dim:#a0a0a8; --fg-mute:#6d6d76;
    --green:#3fb950; --green-dim:rgba(63,185,80,0.12);
    --amber:#d9a441; --amber-dim:rgba(217,164,65,0.12);
    --red:#f85149;   --red-dim:rgba(248,81,73,0.12);
    --blue:#4b8bf5;  --blue-dim:rgba(75,139,245,0.12);
    --sans:'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', sans-serif;
    --mono:'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace;
  }
  * { box-sizing:border-box; }
  body { font-family:var(--sans); background:var(--bg); color:var(--fg); margin:0;
         padding:28px 24px 64px; font-size:14px; line-height:1.45; }
  .wrap { max-width:1180px; margin:0 auto; }
  .mono { font-family:var(--mono); font-variant-numeric:tabular-nums; }
  .muted { color:var(--fg-mute); }

  /* ── header ───────────────────────────────────────────────── */
  .topbar { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; margin-bottom:22px; }
  .eyebrow { font-family:var(--mono); font-size:10.5px; letter-spacing:0.18em; text-transform:uppercase;
             color:var(--fg-mute); margin-bottom:6px; }
  h1 { margin:0 0 6px; font-size:24px; font-weight:600; letter-spacing:-0.01em; }
  .statusline { font-size:12px; color:var(--fg-dim); }
  .topbar-actions { display:flex; gap:8px; flex-shrink:0; }

  /* ── disk strip ───────────────────────────────────────────── */
  .diskstrip { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
               font-family:var(--mono); font-size:12px; color:var(--fg-dim);
               border:1px solid var(--line); border-radius:6px; padding:9px 13px; margin-bottom:18px; }
  .diskstrip .free { color:var(--green); }
  .diskstrip .sep { color:var(--line); }
  .diskstrip.bad { border-color:var(--red); background:var(--red-dim); color:#ffb4af; }

  .banner { border-radius:6px; margin-bottom:18px; font-size:13px; display:none; padding:11px 14px; }
  .banner.warn { display:block; background:var(--amber-dim); border:1px solid rgba(217,164,65,0.45);
                 color:#f0c674; line-height:1.6; }
  .banner-dismiss { background:transparent; border:1px solid var(--amber); color:#f0c674;
                    margin-left:8px; padding:2px 8px; font-size:11px; border-radius:4px; cursor:pointer; }
  .banner-dismiss:hover { background:rgba(217,164,65,0.2); }

  /* ── sections & tables ────────────────────────────────────── */
  section { margin-bottom:34px; }
  .sec-head { font-family:var(--mono); font-size:10.5px; letter-spacing:0.18em; text-transform:uppercase;
              color:var(--fg-mute); margin:0 0 10px; font-weight:400; }
  table { border-collapse:collapse; width:100%; }
  thead th { font-family:var(--mono); font-size:10px; letter-spacing:0.14em; text-transform:uppercase;
             color:var(--fg-mute); font-weight:400; text-align:left;
             padding:0 14px 8px; border-bottom:1px solid var(--line); }
  tbody td { padding:12px 14px; border-bottom:1px solid var(--line-soft); vertical-align:middle; }
  tbody tr:hover > td { background:var(--panel); }
  .ta-r { text-align:right; }
  td.ta-r > * { justify-content:flex-end; }
  .empty { color:var(--fg-mute); padding:18px 14px; }

  /* ── device cell ──────────────────────────────────────────── */
  .dev { display:flex; align-items:flex-start; gap:11px; }
  .dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; margin-top:6px; background:var(--red); }
  tr.online .dot { background:var(--green); box-shadow:0 0 7px rgba(63,185,80,0.7); }
  .dev-name { display:flex; align-items:center; gap:7px; font-size:14px; }
  .dev-sub { font-family:var(--mono); font-size:11.5px; color:var(--fg-mute); margin-top:2px; }
  .name-edit { background:transparent; color:var(--fg); border:1px solid transparent; border-radius:4px;
               padding:2px 5px; margin-left:-5px; font:inherit; width:14ch; min-width:14ch; max-width:26ch; }
  .name-edit:hover { border-color:var(--line); }
  .name-edit:focus { outline:none; border-color:var(--blue); background:var(--panel-2); }
  .tag { font-family:var(--mono); font-size:9.5px; letter-spacing:0.1em; text-transform:uppercase;
         padding:1px 5px; border-radius:3px; }
  .tag.pc { background:var(--green-dim); color:var(--green); border:1px solid rgba(63,185,80,0.35); }
  .tag.nonpc { background:var(--red-dim); color:#f0857c; border:1px solid rgba(248,81,73,0.35); }
  .tag.unknown { background:var(--panel-2); color:var(--fg-mute); border:1px solid var(--line); }

  /* ── buttons ──────────────────────────────────────────────── */
  button { font:inherit; font-size:12px; padding:5px 11px; border-radius:5px;
           border:1px solid var(--line); background:var(--panel-2); color:var(--fg-dim); cursor:pointer; }
  button:hover:not(:disabled) { background:#232329; color:var(--fg); border-color:#33333a; }
  button:disabled { opacity:0.35; cursor:not-allowed; }
  button.primary { background:var(--blue); border-color:var(--blue); color:#fff; }
  button.primary:hover:not(:disabled) { background:#5d99ff; border-color:#5d99ff; color:#fff; }
  button.danger:hover:not(:disabled) { background:var(--red-dim); border-color:var(--red); color:#ff9c95; }
  button.icon { padding:5px 8px; color:var(--fg-mute); line-height:1; }
  .actions { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
  .linkbtn { background:transparent; border:none; padding:0 2px; color:var(--fg-mute);
             font-size:11px; text-decoration:underline; text-underline-offset:2px; }
  .linkbtn:hover:not(:disabled) { background:transparent; color:var(--fg); border:none; }

  /* ── armed pill ───────────────────────────────────────────── */
  .pill { display:inline-flex; align-items:center; gap:7px; font-family:var(--mono); font-size:10.5px;
          letter-spacing:0.1em; text-transform:uppercase; padding:5px 11px; border-radius:20px; }
  .pill.backup   { background:var(--amber-dim); color:var(--amber); border:1px solid rgba(217,164,65,0.45); }
  .pill.recovery { background:var(--blue-dim);  color:#8fb6ff;      border:1px solid rgba(75,139,245,0.45); }
  .pill .ttl { color:var(--fg-dim); letter-spacing:0.05em; }
  .pill-btn { font-family:var(--mono); font-size:11px; letter-spacing:0.04em;
              padding:5px 11px; border-radius:20px; }
  .pill-btn.open { background:var(--blue-dim); border-color:rgba(75,139,245,0.45); color:#8fb6ff; }

  .done-badge { display:inline-flex; align-items:center; padding:4px 9px; border-radius:5px; font-size:11.5px; }
  .done-badge.ok  { background:var(--green-dim); color:var(--green); border:1px solid rgba(63,185,80,0.35); }
  .done-badge.err { background:var(--red-dim); color:#f0857c; border:1px solid rgba(248,81,73,0.4); }

  /* ── backups: groups & versions ───────────────────────────── */
  .mach-name { display:flex; align-items:center; gap:9px; }
  .mach-sub { font-size:11.5px; margin-top:2px; color:var(--fg-mute); }
  .mach-sub.off { color:var(--amber); opacity:0.75; }
  .rename-box { display:flex; align-items:center; gap:6px; }
  .rename-box input { background:var(--panel-2); color:var(--fg); border:1px solid var(--blue);
                      border-radius:4px; padding:4px 7px; font:inherit; font-size:13px; width:20ch; }
  .rename-box input:focus { outline:none; }
  tr.vers > td { padding:0 14px 14px; background:var(--panel); border-bottom:1px solid var(--line-soft); }
  ul.versions { list-style:none; margin:0; padding:0; border:1px solid var(--line); border-radius:6px;
                overflow:hidden; background:var(--bg); }
  ul.versions li { display:flex; align-items:center; gap:12px; padding:9px 13px;
                   border-bottom:1px solid var(--line-soft); font-size:12.5px; }
  ul.versions li:last-child { border-bottom:none; }
  ul.versions li .vid { flex:1; min-width:0; font-size:11.5px; color:var(--fg-dim);
                        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  ul.versions li .vdate, ul.versions li .vsize { font-family:var(--mono); font-size:11.5px;
                        color:var(--fg-mute); white-space:nowrap; }
  ul.versions li .vsize { min-width:170px; text-align:right; }
  .vtag { font-family:var(--mono); font-size:10.5px; letter-spacing:0.06em;
          padding:2px 7px; border-radius:3px; background:var(--panel-2); color:var(--fg-mute);
          border:1px solid var(--line); flex-shrink:0; }
  .vtag.latest { background:var(--green-dim); color:var(--green); border-color:rgba(63,185,80,0.35); }

  .progress { display:flex; flex-direction:column; gap:4px; min-width:170px; }
  .progress .bar { position:relative; height:5px; background:var(--panel-2); border-radius:3px; overflow:hidden; }
  .progress .fill { height:100%; background:var(--blue); transition:width 1s ease; }
  .progress .label { font-family:var(--mono); font-size:11px; color:var(--fg-mute); }
  .progress.indeterminate .fill { width:30% !important; animation:indet 1.4s ease-in-out infinite; }
  @keyframes indet { 0%{ transform:translateX(-100%);} 100%{ transform:translateX(330%);} }

  /* ── modals ───────────────────────────────────────────────── */
  .modal-bg { position:fixed; inset:0; background:rgba(0,0,0,0.72); display:none;
              align-items:center; justify-content:center; z-index:10; padding:24px; }
  .modal-bg.show { display:flex; }
  .modal { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:22px;
           width:560px; max-width:100%; max-height:84vh; overflow:auto; }
  .modal h2 { margin:0 0 5px; font-size:16px; font-weight:600; }
  .modal .sub { color:var(--fg-dim); font-size:12px; margin-bottom:16px; }
  .modal ul.opts { list-style:none; padding:0; margin:0 0 18px; max-height:46vh; overflow:auto; }
  .modal ul.opts li { padding:11px 13px; border:1px solid var(--line); border-radius:6px; margin-bottom:6px;
                      display:flex; align-items:center; gap:11px; }
  .modal ul.opts li[data-name], .modal ul.opts li.pick { cursor:pointer; }
  .modal ul.opts li[data-name]:hover, .modal ul.opts li.pick:hover { background:var(--panel-2); }
  .modal ul.opts li.selected { border-color:var(--blue); background:var(--blue-dim); }
  .modal ul.opts li.empty { border:none; color:var(--fg-mute); }
  .modal .opt-info { flex:1; min-width:0; }
  .modal .opt-name { display:flex; align-items:center; gap:8px; font-size:13px; }
  .modal .opt-meta { font-family:var(--mono); font-size:11px; color:var(--fg-mute); margin-top:2px; }
  .modal .row { display:flex; justify-content:flex-end; gap:8px; }
  .modal .row button { padding:8px 15px; font-size:13px; }
  .opts-toolbar { display:flex; gap:10px; margin-bottom:9px; }
  .modal ul.opts li input[type=checkbox] { accent-color:var(--blue); }
  .modal ul.opts li .opt-name input[type=text] { background:transparent; color:var(--fg);
        border:1px solid transparent; border-radius:4px; padding:2px 5px; font:inherit; width:22ch; }
  .modal ul.opts li .opt-name input[type=text]:hover { border-color:var(--line); }
  .modal ul.opts li .opt-name input[type=text]:focus { outline:none; border-color:var(--blue); background:var(--panel-2); }

  .field { display:block; font-family:var(--mono); font-size:10px; letter-spacing:0.14em;
           text-transform:uppercase; color:var(--fg-mute); margin-bottom:12px; }
  .field input { display:block; width:100%; margin-top:6px; background:var(--bg); color:var(--fg);
                 border:1px solid var(--line); border-radius:5px; padding:8px 10px;
                 font-family:var(--sans); font-size:13px; letter-spacing:normal; text-transform:none; }
  .field input:focus { outline:none; border-color:var(--blue); }
  .note { font-size:12px; color:var(--fg-dim); line-height:1.6; margin:0 0 16px; }
  #updateLog { background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:11px 13px;
               margin:0 0 14px; max-height:44vh; min-height:130px; overflow:auto;
               font-family:var(--mono); font-size:11.5px; line-height:1.55; color:var(--fg-dim);
               white-space:pre-wrap; word-break:break-word; }
  .upd-state { font-size:12px; margin:0 0 12px; padding:7px 11px; border-radius:5px; display:none; }
  .upd-state.running { display:block; background:var(--blue-dim); border:1px solid rgba(75,139,245,0.45); color:#8fb6ff; }
  .upd-state.ok      { display:block; background:var(--green-dim); border:1px solid rgba(63,185,80,0.35); color:var(--green); }
  .upd-state.err     { display:block; background:var(--red-dim); border:1px solid rgba(248,81,73,0.4); color:#f0857c; }

  #toast { position:fixed; bottom:24px; right:24px; background:var(--panel); border:1px solid var(--line);
           padding:12px 16px; border-radius:6px; font-size:13px; opacity:0; transition:opacity .2s;
           max-width:400px; pointer-events:none; }
  #toast.show { opacity:1; }
  #toast.err { border-color:var(--red); }
  #toast.ok  { border-color:var(--green); }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <div class="eyebrow">FTD Aero Recovery Center<span id="versionTag"></span></div>
      <h1>Recovery Status</h1>
      <div class="statusline mono" id="meta">Loading…</div>
    </div>
    <div class="topbar-actions">
      <button id="updateBtn">Update</button>
      <button id="addDevicesBtn">+ Add backup devices</button>
    </div>
  </div>

  <div class="diskstrip" id="storageBanner"></div>
  <div class="banner" id="warnBanner"></div>

  <section>
    <h2 class="sec-head">Devices</h2>
    <table>
      <thead><tr>
        <th>Device</th><th>Last backup</th><th>Images</th><th class="ta-r">Actions</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </section>

  <section>
    <h2 class="sec-head">Backups</h2>
    <table>
      <thead><tr>
        <th>Machine</th><th>MAC address</th><th>Latest</th><th>Size</th><th class="ta-r">Actions</th>
      </tr></thead>
      <tbody id="backupRows"></tbody>
    </table>
  </section>
</div>

<div class="modal-bg" id="restoreModal">
  <div class="modal">
    <h2>Restore which image?</h2>
    <div class="sub" id="restoreModalSub"></div>
    <ul class="opts" id="restoreList"></ul>
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
    <div class="opts-toolbar">
      <button id="addDevicesSelectAll" class="linkbtn">Select all</button>
      <button id="addDevicesSelectPcs" class="linkbtn">Select PCs only</button>
      <button id="addDevicesSelectNone" class="linkbtn">Clear</button>
    </div>
    <ul class="opts" id="addDevicesList"></ul>
    <div class="row">
      <button id="addDevicesCancel">Cancel</button>
      <button id="addDevicesConfirm" class="primary" disabled>Add selected</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="updateModal">
  <div class="modal">
    <h2>Software update</h2>
    <div class="sub">Pulls the latest version from the FTD server and installs it.</div>
    <div id="updateForm">
      <p class="note">
        The update connects through the company VPN (L2TP/IPsec), pulls the
        latest version from the FTD server and disconnects right after. The
        credentials are used once for that connection and are never stored.
        Inside the company network both fields may be left empty.
      </p>
      <label class="field">VPN username
        <input type="text" id="vpnUser" autocomplete="off" spellcheck="false">
      </label>
      <label class="field">VPN password
        <input type="password" id="vpnPass" autocomplete="off">
      </label>
    </div>
    <div class="upd-state" id="updateState"></div>
    <pre id="updateLog" style="display:none"></pre>
    <div class="row">
      <button id="updateAbort" style="display:none">Abort update</button>
      <button id="updateClose">Close</button>
      <button id="updateStartBtn" class="primary">Connect &amp; update</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
let serverNow = 0;
let lastFetch = 0;
let lastDeviceCount = 0;
let lastImageCount = 0;
let lastCheckedAt = 0;
const openGroups = new Set();   // MACs whose version list is expanded
let renamingMac = null;         // MAC whose name cell is in edit mode

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

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024**2) return (n/1024).toFixed(1) + ' KB';
  if (n < 1024**3) return (n/1024**2).toFixed(1) + ' MB';
  return (n/1024**3).toFixed(1) + ' GB';
}

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString([], {
    year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit'
  });
}

function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderMeta() {
  if (!lastCheckedAt) return;
  document.getElementById('meta').textContent =
    'Last check ' + new Date(lastCheckedAt * 1000).toLocaleTimeString() +
    ' · ' + plural(lastDeviceCount, 'device') +
    ' · ' + plural(lastImageCount, 'image');
}

// ── device actions ───────────────────────────────────────────────────────
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
  btn.disabled = true;
  try {
    const r = await fetch(`/api/host/${encodeURIComponent(ip)}`, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'remove failed');
    toast(`Removed ${d.removed.name || ip}`, 'ok');
    refresh();
  } catch (e) { toast('Error: ' + e.message, 'err'); btn.disabled = false; }
}

// ── restore picker ───────────────────────────────────────────────────────
function versionTag(img, isOwn) {
  const v = 'v' + (img.version || 1);
  if (isOwn) return `<span class="vtag latest">THIS HOST · ${v}</span>`;
  if (img.version === img.version_count && img.version_count > 1)
    return `<span class="vtag latest">${v} · latest</span>`;
  return `<span class="vtag">${v}</span>`;
}

async function openRestorePicker(host, btn, presetImage) {
  const modal   = document.getElementById('restoreModal');
  const list    = document.getElementById('restoreList');
  const sub     = document.getElementById('restoreModalSub');
  const confirm = document.getElementById('restoreConfirm');
  const cancel  = document.getElementById('restoreCancel');
  sub.textContent = [host.name, host.host, host.mac || 'MAC unknown'].join(' · ');
  list.innerHTML = '<li class="empty">Loading…</li>';
  modal.classList.add('show');
  confirm.disabled = true;
  const close = () => modal.classList.remove('show');
  cancel.onclick = close;
  modal.onclick = e => { if (e.target === modal) close(); };
  let selected = null;
  const select = (name) => { selected = name; confirm.disabled = !name; };
  try {
    const r = await fetch('/api/images', { cache: 'no-store' });
    const d = await r.json();
    if (!d.images.length) {
      list.innerHTML = '<li class="empty">No images available.</li>';
      return;
    }
    const ownMac = (host.mac || '').toLowerCase();
    const isOwn = (img) => !!ownMac && img.mac === ownMac;
    list.innerHTML = d.images.map(img => {
      const label = img.machine_name || img.name;
      const metaBits = [fmtDate(img.mtime), fmtBytes(img.size_bytes)];
      if (img.mac) metaBits.push(img.mac);
      return `
        <li data-name="${escapeHtml(img.name)}">
          <div class="opt-info">
            <div class="opt-name">${escapeHtml(label)} ${versionTag(img, isOwn(img))}</div>
            <div class="opt-meta">${escapeHtml(metaBits.join(' · '))}</div>
          </div>
        </li>`;
    }).join('');
    list.querySelectorAll('li[data-name]').forEach(li => {
      li.addEventListener('click', () => {
        list.querySelectorAll('li').forEach(x => x.classList.remove('selected'));
        li.classList.add('selected');
        select(li.dataset.name);
      });
    });
    // Pre-select: an explicitly requested version, else this host's newest image
    // (api_images returns newest-first).
    const preset = presetImage
      ? d.images.find(i => i.name === presetImage)
      : d.images.find(isOwn);
    if (preset) {
      select(preset.name);
      const li = list.querySelector(`li[data-name="${CSS.escape(preset.name)}"]`);
      if (li) li.classList.add('selected');
    }
  } catch (e) {
    list.innerHTML = '<li class="empty">Error loading images: ' + escapeHtml(e.message) + '</li>';
  }
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

// ── devices table ────────────────────────────────────────────────────────
function buildActionsCell(h, storageOk) {
  // Only block actions while the job is actively running. Terminal states
  // (completed/failed) show a small badge alongside the normal buttons so the
  // user can immediately re-trigger or remove the host.
  const p = h.progress;
  const terminal = p && (p.status === 'completed' || p.status === 'failed');
  if (h.armed) {
    const m = h.armed.mode;
    const remaining = Math.max(0, h.armed.expires_at - serverNow);
    const verb = m === 'recovery' ? 'Restore' : 'Backup';
    return `
      <div class="actions">
        <span class="pill ${m}" data-expires="${h.armed.expires_at}">
          ${verb} armed <span class="ttl">· ${fmtTtl(remaining)}</span>
        </span>
        <button class="disarm" data-ip="${h.host}" data-action="disarm">Disarm</button>
      </div>`;
  }
  const macAttr = h.mac ? '' : 'disabled title="No MAC known"';
  const backupAttr = h.mac
    ? (storageOk ? '' : 'disabled title="Backup storage offline — fix the storage banner first"')
    : 'disabled title="No MAC known"';
  let terminalBadge = '';
  if (terminal) {
    const phase = (p.phase || '').toLowerCase();
    const verb = phase === 'restore' ? 'Restore' : (phase === 'backup' ? 'Backup' : 'Job');
    if (p.status === 'completed') {
      terminalBadge = `<span class="done-badge ok" title="${verb} completed">✓ ${verb} done</span>
        <button class="icon" data-ip="${h.host}" data-mac="${h.mac || ''}" data-action="dismiss-progress" title="Dismiss">✕</button>`;
    } else {
      const rc = p.rc != null ? ` (rc=${p.rc})` : '';
      terminalBadge = `<span class="done-badge err" title="${verb} failed${rc}">✗ ${verb} failed${rc}</span>
        <button class="icon" data-ip="${h.host}" data-mac="${h.mac || ''}" data-action="dismiss-progress" title="Dismiss">✕</button>`;
    }
  }
  return `
    <div class="actions">
      ${terminalBadge}
      <button data-ip="${h.host}" data-action="wol"      ${macAttr}>Wake</button>
      <button data-ip="${h.host}" data-action="recovery" ${macAttr}>Restore</button>
      <button data-ip="${h.host}" data-action="backup"   ${backupAttr}>Backup</button>
      <button class="icon danger" data-ip="${h.host}" data-action="remove" title="Remove from backup list">✕</button>
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
    lastCheckedAt = d.checked_at;
    lastDeviceCount = d.hosts.length;
    // Backup groups are keyed by MAC; this is how a group finds the live host
    // it can be restored onto.
    window._hostsByMac = {};
    d.hosts.forEach(h => { if (h.mac) window._hostsByMac[h.mac] = h; });
    const rows = document.getElementById('rows');
    if (!d.hosts.length) {
      rows.innerHTML = '<tr><td colspan="4" class="empty">No devices in the backup list yet. Click "+ Add backup devices" to scan and pick.</td></tr>';
    } else {
      const storageOk = !!(d.storage && d.storage.ok);
      rows.innerHTML = d.hosts.map(h => {
        const safeName = escapeHtml(h.name);
        const tag = h.category === 'pc'
          ? '<span class="tag pc" title="MAC vendor is a known PC NIC">PC</span>' : '';
        const subBits = [h.host];
        if (h.mac) subBits.push(h.mac + (h.mac_source === 'arp' ? ' (arp)' : ''));
        else subBits.push('no MAC');
        if (h.latency_ms != null) subBits.push(h.latency_ms.toFixed(1) + ' ms');
        const backupCell = h.last_backup_at
          ? `<span class="mono" title="${escapeHtml(new Date(h.last_backup_at * 1000).toLocaleString())}">${fmtRelative(serverNow - h.last_backup_at)}</span>`
          : '<span class="muted">never</span>';
        const imagesCell = h.image_count
          ? `<span class="mono">${plural(h.image_count, 'image')}</span>`
          : '<span class="muted">—</span>';
        return `
          <tr class="${h.online ? 'online' : 'offline'}">
            <td>
              <div class="dev">
                <span class="dot" title="${h.online ? 'Online' : 'Offline'}"></span>
                <div>
                  <div class="dev-name">
                    <input class="name-edit" data-ip="${h.host}" data-original="${safeName}" value="${safeName}" size="1">${tag}
                  </div>
                  <div class="dev-sub">${escapeHtml(subBits.join(' · '))}</div>
                </div>
              </div>
            </td>
            <td>${backupCell}</td>
            <td>${imagesCell}</td>
            <td class="ta-r">${buildActionsCell(h, storageOk)}</td>
          </tr>`;
      }).join('');

      rows.querySelectorAll('.name-edit').forEach(inp => {
        const fit = () => { inp.style.width = Math.max(8, Math.min(26, inp.value.length + 1)) + 'ch'; };
        fit();
        inp.addEventListener('input', fit);
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
    renderMeta();
    const banner = document.getElementById('storageBanner');
    const s = d.storage || {};
    window._lastStorage = s;
    if (!s.ok) {
      banner.className = 'diskstrip bad';
      banner.textContent = `⚠ Backup drive offline — ${s.error || 'unknown error'}. Backups are paused.`;
    } else {
      renderStorageBanner(banner, s);
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
  document.querySelectorAll('.pill[data-expires]').forEach(el => {
    const remaining = Math.max(0, parseFloat(el.dataset.expires) - now);
    const ttl = el.querySelector('.ttl');
    if (ttl) ttl.textContent = fmtTtl(remaining);
    if (remaining === 0) refresh();
  });
}, 1000);

// ── backups table (grouped by MAC, newest version first) ─────────────────
function groupImages(images) {
  const groups = new Map();
  images.forEach(img => {
    const key = img.mac || ('name:' + img.name);
    let g = groups.get(key);
    if (!g) {
      g = { key, mac: img.mac, images: [], machine_name: null, on_network: false, host_ip: null };
      groups.set(key, g);
    }
    g.images.push(img);
    if (img.machine_name && !g.machine_name) g.machine_name = img.machine_name;
    if (img.on_network) { g.on_network = true; g.host_ip = img.host_ip; }
  });
  groups.forEach(g => {
    g.images.sort((a, b) => b.mtime - a.mtime);   // newest version first
    g.latest = g.images[0];
  });
  return [...groups.values()].sort((a, b) => b.latest.mtime - a.latest.mtime);
}

function sizeCellHtml(img) {
  if (img.in_progress && img.estimated_percent != null) {
    const pct = img.estimated_percent.toFixed(1);
    return `
      <div class="progress">
        <div class="bar"><div class="fill" style="width:${pct}%"></div></div>
        <div class="label">${fmtBytes(img.size_bytes)} / ~${fmtBytes(img.reference_size)} · ${pct}%</div>
      </div>`;
  }
  if (img.in_progress) {
    return `
      <div class="progress indeterminate">
        <div class="bar"><div class="fill"></div></div>
        <div class="label">${fmtBytes(img.size_bytes)} · running (no prior backup for reference)</div>
      </div>`;
  }
  return `<span class="mono">${fmtBytes(img.size_bytes)}</span>`;
}

function machineCellHtml(g) {
  const name = g.machine_name || g.latest.name;
  // Every button here is looked up by data-key in the delegated click handler,
  // so all of them must carry it.
  const key = escapeHtml(g.key);
  if (renamingMac === g.key) {
    return `
      <div class="rename-box">
        <input type="text" class="rename-input" value="${escapeHtml(name)}" maxlength="64">
        <button data-action="rename-save" data-key="${key}">Save</button>
        <button data-action="rename-cancel" data-key="${key}">Cancel</button>
      </div>`;
  }
  const sub = g.on_network
    ? `<div class="mach-sub">on the network · ${escapeHtml(g.host_ip || '')}</div>`
    : '<div class="mach-sub off">not on the network · name kept from backup</div>';
  const renameBtn = g.mac
    ? `<button class="linkbtn" data-action="rename" data-key="${key}">rename</button>`
    : '';
  return `
    <div class="mach-name"><span>${escapeHtml(name)}</span>${renameBtn}</div>
    ${sub}`;
}

async function saveMachineName(mac, name) {
  try {
    const r = await fetch(`/api/machine/${encodeURIComponent(mac)}/name`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'rename failed');
    toast('Renamed to ' + d.name, 'ok');
  } catch (e) { toast('Error: ' + e.message, 'err'); }
}

async function deleteImage(name) {
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

async function refreshBackups() {
  let d;
  try {
    const r = await fetch('/api/images', { cache: 'no-store' });
    d = await r.json();
  } catch (e) { return; }
  lastImageCount = d.images.length;
  renderMeta();
  const rows = document.getElementById('backupRows');
  if (!d.images.length) {
    rows.innerHTML = '<tr><td colspan="5" class="empty">No backups yet.</td></tr>';
    return;
  }
  const groups = groupImages(d.images);
  rows.innerHTML = groups.map(g => {
    const expanded = openGroups.has(g.key);
    const n = g.images.length;
    const restoreAttr = g.on_network ? '' : 'disabled title="Machine is not on the network"';
    const versions = g.images.map(img => {
      const isLatest = img === g.latest;
      return `
        <li>
          <span class="vtag ${isLatest ? 'latest' : ''}">v${img.version || 1}</span>
          <span class="vid mono">${escapeHtml(img.name)}</span>
          <span class="vdate">${fmtDate(img.mtime)}</span>
          <div class="vsize">${sizeCellHtml(img)}</div>
          <button data-action="restore-version" data-key="${escapeHtml(g.key)}" data-name="${escapeHtml(img.name)}" ${restoreAttr}>Restore</button>
          <button class="icon danger" data-action="delete-image" data-name="${escapeHtml(img.name)}" title="Delete this image">✕</button>
        </li>`;
    }).join('');
    return `
      <tr data-key="${escapeHtml(g.key)}">
        <td>${machineCellHtml(g)}</td>
        <td class="mono">${escapeHtml(g.mac || '—')}</td>
        <td class="mono">${fmtDate(g.latest.mtime)}</td>
        <td>${sizeCellHtml(g.latest)}</td>
        <td class="ta-r"><div class="actions">
          <button data-action="restore-version" data-key="${escapeHtml(g.key)}" data-name="${escapeHtml(g.latest.name)}" ${restoreAttr}>Restore</button>
          <button class="pill-btn ${expanded ? 'open' : ''}" data-action="toggle" data-key="${escapeHtml(g.key)}">${plural(n, 'version')}</button>
        </div></td>
      </tr>
      <tr class="vers" data-key="${escapeHtml(g.key)}" ${expanded ? '' : 'hidden'}>
        <td colspan="5"><ul class="versions">${versions}</ul></td>
      </tr>`;
  }).join('');

  const groupByKey = new Map(groups.map(g => [g.key, g]));
  const commitRename = (g, value) => {
    renamingMac = null;
    const name = (value || '').trim();
    const current = g.machine_name || g.latest.name;
    if (name && name !== current) {
      saveMachineName(g.mac, name).then(refreshBackups);
    } else {
      refreshBackups();
    }
  };

  rows.querySelectorAll('button[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const a = btn.dataset.action;
      const g = groupByKey.get(btn.dataset.key);
      // delete-image works off data-name alone; everything else needs its group.
      if (!g && a !== 'delete-image') return;
      if (a === 'toggle') {
        if (openGroups.has(g.key)) openGroups.delete(g.key); else openGroups.add(g.key);
        refreshBackups();
      } else if (a === 'rename') {
        renamingMac = g.key;
        refreshBackups();
      } else if (a === 'rename-cancel') {
        renamingMac = null;
        refreshBackups();
      } else if (a === 'rename-save') {
        commitRename(g, btn.closest('.rename-box').querySelector('input').value);
      } else if (a === 'delete-image') {
        deleteImage(btn.dataset.name);
      } else if (a === 'restore-version') {
        const host = (window._hostsByMac || {})[g.mac];
        if (!host) { toast('That machine is not on the network', 'err'); return; }
        openRestorePicker(host, btn, btn.dataset.name);
      }
    });
  });

  const editor = rows.querySelector('.rename-input');
  if (editor) {
    const g = groupByKey.get(renamingMac);
    editor.focus();
    editor.select();
    editor.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); commitRename(g, editor.value); }
      if (e.key === 'Escape') { renamingMac = null; refreshBackups(); }
    });
  }
}

// ── disk strip ───────────────────────────────────────────────────────────
let lastDriveHealth = null;

function renderStorageBanner(banner, s) {
  const h = lastDriveHealth;
  if (h && h.ok && h.health === 'FAILED') {
    banner.className = 'diskstrip bad';
    banner.textContent = `⚠ Drive health FAILED — ${h.model || 'backup drive'}. Consider replacing it before data is lost.`;
    return;
  }
  const parts = [`<span class="free">${s.free_gb} GB free</span>`, `of ${s.total_gb} GB`];
  if (h && h.ok) {
    if (h.model) parts.push(escapeHtml(h.model));
    if (h.health === 'PASSED')       parts.push('SMART OK');
    else if (h.health === 'UNKNOWN') parts.push('SMART unavailable');
    if (h.temperature_c != null)     parts.push(`${h.temperature_c}°C`);
    if (h.power_on_hours != null)    parts.push(`${h.power_on_hours.toLocaleString()} hrs on`);
    if (h.nvme)                      parts.push(`${h.nvme.available_spare_pct}% spare`);
  }
  banner.className = 'diskstrip';
  banner.innerHTML = parts.join('<span class="sep">·</span>');
}

async function refreshDriveHealth() {
  try {
    const r = await fetch('/api/drive-health');
    if (!r.ok) return;
    const h = await r.json();
    lastDriveHealth = h.ok ? h : null;
    const banner = document.getElementById('storageBanner');
    const s = window._lastStorage;
    if (s && s.ok) renderStorageBanner(banner, s);
  } catch (e) { /* silent */ }
}

// ── add backup devices ───────────────────────────────────────────────────
async function openAddDevicesPicker(btn) {
  const modal   = document.getElementById('addDevicesModal');
  const list    = document.getElementById('addDevicesList');
  const sub     = document.getElementById('addDevicesSub');
  const confirm = document.getElementById('addDevicesConfirm');
  const cancel  = document.getElementById('addDevicesCancel');
  const selAll  = document.getElementById('addDevicesSelectAll');
  const selPcs  = document.getElementById('addDevicesSelectPcs');
  const selNone = document.getElementById('addDevicesSelectNone');
  sub.textContent = 'Scanning the subnet — running arp-scan…';
  list.innerHTML = '';
  confirm.disabled = true;
  modal.classList.add('show');
  const close = () => modal.classList.remove('show');
  cancel.onclick = close;
  modal.onclick = e => { if (e.target === modal) close(); };
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
    warnings = { replaced: d.replaced || [], removed: d.removed || [], moved: d.moved || [] };
  } catch (e) {
    list.innerHTML = `<li class="empty">Scan failed: ${escapeHtml(e.message)}</li>`;
    sub.textContent = '';
    btn.disabled = false;
    return;
  } finally { btn.disabled = false; }
  if (!discovered.length) {
    sub.textContent = '';
    list.innerHTML = '<li class="empty">No new devices discovered. Everything on the subnet is either already in the list or this host itself.</li>';
  } else {
    sub.textContent = `Found ${discovered.length} device(s) not yet in the backup list. Tick the ones you want to back up.`;
    list.innerHTML = discovered.map((dev, i) => {
      const badge = dev.category === 'pc' ? '<span class="tag pc">PC</span>'
                  : dev.category === 'nonpc' ? '<span class="tag nonpc">non-PC</span>'
                  : '<span class="tag unknown">?</span>';
      const safeName = escapeHtml(dev.name || '');
      return `
        <li class="pick" data-i="${i}">
          <input type="checkbox" data-i="${i}">
          <div class="opt-info">
            <div class="opt-name">
              <input type="text" data-i="${i}" data-field="name" value="${safeName}">
              ${badge}
            </div>
            <div class="opt-meta">${escapeHtml(dev.host)} · ${escapeHtml(dev.mac)}${dev.vendor ? ' · ' + escapeHtml(dev.vendor) : ''}</div>
          </div>
        </li>`;
    }).join('');
    const updateConfirm = () => {
      const n = list.querySelectorAll('input[type=checkbox]:checked').length;
      confirm.disabled = n === 0;
      confirm.textContent = n ? `Add ${n} selected` : 'Add selected';
    };
    list.querySelectorAll('li.pick').forEach(row => {
      const cb = row.querySelector('input[type=checkbox]');
      row.addEventListener('click', e => {
        if (e.target.tagName === 'INPUT') return;  // let inputs handle themselves
        cb.checked = !cb.checked;
        row.classList.toggle('selected', cb.checked);
        updateConfirm();
      });
      cb.addEventListener('change', () => {
        row.classList.toggle('selected', cb.checked);
        updateConfirm();
      });
    });
    const setAll = (predicate) => {
      list.querySelectorAll('li.pick').forEach(row => {
        const i = parseInt(row.dataset.i, 10);
        const cb = row.querySelector('input[type=checkbox]');
        cb.checked = predicate(discovered[i]);
        row.classList.toggle('selected', cb.checked);
      });
      updateConfirm();
    };
    selAll.onclick  = () => setAll(_ => true);
    selPcs.onclick  = () => setAll(d => d.category === 'pc');
    selNone.onclick = () => setAll(_ => false);
  }
  // Surface MAC-change / dedup warnings even if no devices were discovered.
  const warn = document.getElementById('warnBanner');
  if (warnings && (warnings.replaced.length || warnings.removed.length || warnings.moved.length)) {
    warn.className = 'banner warn';
    const parts = [];
    if (warnings.moved.length) {
      parts.push('<strong>IP address changed — entries updated to follow the device:</strong>');
      parts.push(warnings.moved.map(r =>
        `&nbsp;&nbsp;"${escapeHtml(r.name || r.mac)}": ${escapeHtml(r.old_host)} → ${escapeHtml(r.new_host)}`
      ).join('<br>'));
    }
    if (warnings.replaced.length) {
      if (parts.length) parts.push('<br>');
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
  confirm.onclick = async () => {
    const picks = [];
    list.querySelectorAll('li.pick').forEach(row => {
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

// ── software update ──────────────────────────────────────────────────────
const updModal = document.getElementById('updateModal');
const updForm  = document.getElementById('updateForm');
const updLog   = document.getElementById('updateLog');
const updState = document.getElementById('updateState');
const updStart = document.getElementById('updateStartBtn');
const updAbort = document.getElementById('updateAbort');
let updTimer = null;
let updWasRunning = false;

function updSetView(mode) {  // 'form' | 'log'
  updForm.style.display  = mode === 'form' ? '' : 'none';
  updLog.style.display   = mode === 'log'  ? '' : 'none';
  updStart.style.display = mode === 'form' ? '' : 'none';
  updAbort.style.display = mode === 'log'  ? '' : 'none';
}

function updStopPolling() {
  if (updTimer) { clearInterval(updTimer); updTimer = null; }
}

async function updPoll() {
  let d;
  try {
    const r = await fetch('/api/update/status', { cache: 'no-store' });
    d = await r.json();
  } catch (e) {
    // The interface restarts while update.sh runs — keep polling until it's back.
    updState.className = 'upd-state running';
    updState.textContent = 'Interface restarting — waiting for it to come back…';
    return;
  }
  const s = d.status || {};
  const atBottom = updLog.scrollTop + updLog.clientHeight >= updLog.scrollHeight - 8;
  updLog.textContent = d.log || '';
  if (atBottom) updLog.scrollTop = updLog.scrollHeight;
  if (s.state === 'running') {
    updWasRunning = true;
    updState.className = 'upd-state running';
    updState.textContent = (s.message || s.step || 'running') + '…';
  } else if (s.state === 'success' && updWasRunning) {
    updStopPolling();
    updAbort.style.display = 'none';
    updState.className = 'upd-state ok';
    updState.textContent = (s.message || 'Update complete') + ' — reloading…';
    setTimeout(() => location.reload(), 2500);
  } else if (s.state === 'failed' && updWasRunning) {
    updStopPolling();
    updAbort.style.display = 'none';
    updState.className = 'upd-state err';
    updState.textContent = s.message || 'Update failed — see log';
  }
}

function openUpdateModal() {
  updModal.classList.add('show');
  updState.className = 'upd-state';
  fetch('/api/update/status', { cache: 'no-store' }).then(r => r.json()).then(d => {
    if (d.status && d.status.state === 'running') {
      updWasRunning = true;
      updSetView('log');
      updPoll();
      if (!updTimer) updTimer = setInterval(updPoll, 2000);
    } else {
      updWasRunning = false;
      updSetView('form');
    }
  }).catch(() => { updWasRunning = false; updSetView('form'); });
}

function updCloseModal() {
  updStopPolling();
  updModal.classList.remove('show');
}

updStart.addEventListener('click', async () => {
  updStart.disabled = true;
  const username = document.getElementById('vpnUser').value;
  const password = document.getElementById('vpnPass').value;
  try {
    const r = await fetch('/api/update', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'failed to start update');
    document.getElementById('vpnPass').value = '';
    updWasRunning = true;
    updLog.textContent = '';
    updSetView('log');
    updState.className = 'upd-state running';
    updState.textContent = 'Connecting to the VPN…';
    if (!updTimer) updTimer = setInterval(updPoll, 2000);
  } catch (e) {
    updState.className = 'upd-state err';
    updState.textContent = e.message;
  } finally { updStart.disabled = false; }
});

updAbort.addEventListener('click', async () => {
  if (!window.confirm('Abort the running update and disconnect the VPN?')) return;
  updAbort.disabled = true;
  try {
    const r = await fetch('/api/update/abort', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'abort failed');
    toast('Update aborted', 'ok');
  } catch (e) { toast('Error: ' + e.message, 'err'); }
  finally { updAbort.disabled = false; }
});

document.getElementById('updateClose').addEventListener('click', updCloseModal);
updModal.addEventListener('click', e => { if (e.target === updModal) updCloseModal(); });
document.getElementById('updateBtn').addEventListener('click', openUpdateModal);

// ── boot ─────────────────────────────────────────────────────────────────
fetch('/api/version').then(r => r.json()).then(d => {
  document.getElementById('versionTag').textContent = ' · v' + d.version;
}).catch(() => {});

refresh();
refreshBackups();
refreshDriveHealth();
setInterval(refresh, 5000);
// Skip the periodic re-render while a name is being edited — it would replace
// the input under the operator's cursor.
setInterval(() => { if (renamingMac === null) refreshBackups(); }, 5000);
setInterval(refreshDriveHealth, 60000);
</script>
</body>
</html>
"""
