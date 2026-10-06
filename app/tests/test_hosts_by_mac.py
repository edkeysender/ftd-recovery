"""Tests for MAC-only device entries (bare-metal PCs added by hand)."""
import asyncio

import pytest

import app.app as appmod
from app.app import HostBatchAdd, HostEntry, add_hosts, find_host, host_key


@pytest.fixture
def hosts_file(tmp_path, monkeypatch):
    path = tmp_path / "hosts.yml"
    monkeypatch.setattr(appmod, "HOSTS_FILE", path)
    monkeypatch.setattr(appmod, "DHCP_NAMES_FILE", tmp_path / "dhcp_names.json")
    return path


def test_host_key_prefers_ip_then_mac():
    assert host_key({"host": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:ff"}) == "10.0.0.5"
    assert host_key({"host": "", "mac": "aa:bb:cc:dd:ee:ff"}) == "aa:bb:cc:dd:ee:ff"


def test_find_host_by_ip_or_any_mac_notation():
    hosts = [{"host": "10.0.0.5", "mac": "aa:bb:cc:dd:ee:01"},
             {"host": "", "mac": "50:eb:f6:79:c5:54"}]
    assert find_host(hosts, "10.0.0.5") is hosts[0]
    assert find_host(hosts, "50-EB-F6-79-C5-54") is hosts[1]
    assert find_host(hosts, "50ebf679c554") is hosts[1]
    assert find_host(hosts, "10.0.0.9") is None
    assert find_host(hosts, "") is None


def test_add_mac_only_entry(hosts_file):
    r = asyncio.run(add_hosts(HostBatchAdd(hosts=[HostEntry(mac="50-EB-F6-79-c5-54", name="New PC")])))
    assert r["added"] == [{"name": "New PC", "host": "", "mac": "50:eb:f6:79:c5:54"}]
    assert appmod.load_hosts() == [{"name": "New PC", "host": "", "mac": "50:eb:f6:79:c5:54"}]


def test_two_mac_only_entries_do_not_collide_on_empty_ip(hosts_file):
    asyncio.run(add_hosts(HostBatchAdd(hosts=[HostEntry(mac="50:eb:f6:79:c5:54")])))
    r = asyncio.run(add_hosts(HostBatchAdd(hosts=[HostEntry(mac="50:eb:f6:79:c5:55")])))
    assert len(r["added"]) == 1 and not r["skipped"]


def test_duplicate_mac_and_bad_ip_are_skipped(hosts_file):
    asyncio.run(add_hosts(HostBatchAdd(hosts=[HostEntry(mac="50:eb:f6:79:c5:54")])))
    r = asyncio.run(add_hosts(HostBatchAdd(hosts=[
        HostEntry(mac="50eb.f679.c554", host="10.0.0.7"),
        HostEntry(mac="50:eb:f6:79:c5:99", host="not-an-ip"),
    ])))
    assert [s["reason"] for s in r["skipped"]] == ["already in list", "invalid IP"]
    assert not r["added"]


def test_load_hosts_tolerates_missing_host_key(hosts_file):
    hosts_file.write_text("hosts:\n- name: Bare\n  mac: 50:eb:f6:79:c5:54\n")
    assert appmod.load_hosts()[0]["host"] == ""
