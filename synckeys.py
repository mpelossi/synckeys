#!/usr/bin/env python3

import argparse
import codecs
import configparser
from datetime import datetime
import os
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

WINDOWS_BT_REGISTER_PATH = r"ControlSet001\Services\BTHPORT\Parameters\Keys"


def find_windows_mount():
    """Auto-detect mounted Windows partition via /proc/mounts"""
    mounts = []
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mnt = codecs.decode(parts[1], "unicode_escape")
                    for p in (
                        os.path.join(
                            mnt, "Windows", "System32", "config", "SYSTEM"
                        ),
                        os.path.join(
                            mnt, "windows", "system32", "config", "system"
                        ),
                    ):
                        if os.path.isfile(p) and mnt not in mounts:
                            mounts.append(mnt)
    except Exception:
        pass
    return mounts


def export_registry(windows_root):
    candidates = [
        os.path.join(windows_root, "Windows", "System32", "config", "SYSTEM"),
        os.path.join(windows_root, "windows", "system32", "config", "system"),
    ]
    hive_path = next((c for c in candidates if os.path.isfile(c)), None)
    if not hive_path:
        print(f"[-] ERROR: SYSTEM hive not found in {windows_root}")
        sys.exit(1)

    with TemporaryDirectory() as temp_dir:
        out_reg = os.path.join(temp_dir, "exported.reg")
        cmd = [
            "reged",
            "-x",
            hive_path,
            "HKEY_LOCAL_MACHINE\\SYSTEM",
            WINDOWS_BT_REGISTER_PATH,
            out_reg,
        ]
        res = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        if res.returncode != 0 or not os.path.isfile(out_reg):
            print("[-] ERROR: reged failed to export registry keys.")
            if res.stderr:
                print(res.stderr.decode())
            sys.exit(1)

        with open(out_reg, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()


_prev_adapter_mac = None


def format_hex(s):
    return s.replace("hex:", "").replace(",", "").upper()


def format_hex_b(s):
    parts = s.replace("hex(b):", "").split(",")
    parts.reverse()
    return "".join(parts).upper()


def format_dword(s):
    return s.replace("dword:", "")


def format_mac(s):
    s = s.upper()
    return ":".join(s[i : i + 2] for i in range(0, len(s), 2))


def is_mac_address(name):
    return re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", name) is not None


def get_adapter_path(adapter_mac):
    return f"/var/lib/bluetooth/{adapter_mac}"


def get_device_path(adapter_mac, device_mac):
    return f"/var/lib/bluetooth/{adapter_mac}/{device_mac}"


def get_device_pairing_info(adapter_mac, device_mac):
    info_file = f"{get_device_path(adapter_mac, device_mac)}/info"
    if not os.path.isfile(info_file):
        return None
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    cfg.read(info_file)
    return cfg


def backup_device_info_file(adapter_mac, device_mac):
    path = get_device_path(adapter_mac, device_mac)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copyfile(f"{path}/info", f"{path}/info-{ts}")


def update_system_pairing(adapter_mac, device_mac, config):
    backup_device_info_file(adapter_mac, device_mac)
    with open(
        f"{get_device_path(adapter_mac, device_mac)}/info", "w"
    ) as info_file:
        config.write(info_file)


def print_device_info(device_config, device_mac):
    if not device_config:
        print(f"  {device_mac} (# not paired in Linux #)")
        return
    name = device_config.get("General", "Name", fallback="Unknown")
    alias = device_config.get("General", "Alias", fallback=name)
    print(f"\n  {device_mac} ({name} / {alias})")


def print_update_values(name, current_value, new_value):
    if current_value == new_value:
        print(f"    | {name}: {current_value} > No change required.")
        return False
    print(f"    | {name}: {current_value} > Update to: {new_value}")
    return True


def find_le_clone_candidates(adapter_mac, dump_macs):
    adapter_path = get_adapter_path(adapter_mac)
    candidates = []
    if not os.path.isdir(adapter_path):
        return candidates
    for name in sorted(os.listdir(adapter_path)):
        if not is_mac_address(name) or name in dump_macs:
            continue
        cfg = get_device_pairing_info(adapter_mac, name)
        if not cfg or "General" not in cfg:
            continue
        if "LE" not in cfg["General"].get("SupportedTechnologies", ""):
            continue
        candidates.append((name, cfg))
    return candidates


def clone_le_device(adapter_config, adapter_mac, device_mac, dump_macs):
    candidates = find_le_clone_candidates(adapter_mac, dump_macs)
    if not candidates:
        return None
    windows_irk = (
        format_hex(adapter_config["IRK"]) if "IRK" in adapter_config else None
    )
    suggested = None
    print("    | Linux-paired LE devices not present in Windows:")
    for idx, (mac, cfg) in enumerate(candidates, start=1):
        name = cfg["General"].get("Name", "?")
        marker = ""
        if (
            windows_irk
            and "IdentityResolvingKey" in cfg
            and cfg["IdentityResolvingKey"].get("Key", "").upper()
            == windows_irk
        ):
            marker = " (IRK matches!)"
            suggested = idx
        print(f"    |   {idx}) {mac} ({name}){marker}")

    default = str(suggested) if suggested else "N"
    action = input(
        f"    > Copy one of these pairings to {device_mac}? (number/y/N) [{default}]: "
    ).strip()

    if not action:
        action = default
    # Handle user typing 'y' or 'yes'
    if action.lower() in ("y", "yes"):
        action = str(suggested) if suggested else "1"

    if not action.isdigit() or not 1 <= int(action) <= len(candidates):
        return None

    src_mac = candidates[int(action) - 1][0]
    src_path = get_device_path(adapter_mac, src_mac)
    dst_path = get_device_path(adapter_mac, device_mac)
    shutil.copytree(src_path, dst_path)

    cache_src = f"{get_adapter_path(adapter_mac)}/cache/{src_mac}"
    if os.path.isfile(cache_src):
        shutil.copyfile(
            cache_src, f"{get_adapter_path(adapter_mac)}/cache/{device_mac}"
        )

    paired_cfg = get_device_pairing_info(adapter_mac, device_mac)
    if "AddressType" in adapter_config and "General" in paired_cfg:
        addr_type = (
            "static"
            if int(format_dword(adapter_config["AddressType"]), 16) == 1
            else "public"
        )
        paired_cfg["General"]["AddressType"] = addr_type
        with open(f"{dst_path}/info", "w") as f:
            paired_cfg.write(f)
    print(f"    > Copied {src_mac} -> {device_mac}. Original pairing preserved.")
    return get_device_pairing_info(adapter_mac, device_mac)


def process_basic_pairing(adapter_config, adapter_mac):
    for device, key_val in adapter_config.items():
        if device in ("masterirk", "centralirk"):
            continue
        dev_mac = format_mac(device)
        pairing_key = format_hex(key_val)

        paired_cfg = get_device_pairing_info(adapter_mac, dev_mac)
        print_device_info(paired_cfg, dev_mac)
        if not paired_cfg:
            continue

        cur_key = paired_cfg.get("LinkKey", "Key", fallback=None)
        if not print_update_values("LinkKey", cur_key, pairing_key):
            continue

        if (
            input("    > Update keys for device? (y/N): ").strip().lower()
            == "y"
        ):
            paired_cfg["LinkKey"]["Key"] = pairing_key
            update_system_pairing(adapter_mac, dev_mac, paired_cfg)
            print("    > OK!")


def process_advanced_pairing(
    adapter_config, adapter_mac, device_mac, dump_macs
):
    paired_cfg = get_device_pairing_info(adapter_mac, device_mac)
    print_device_info(paired_cfg, device_mac)

    if not paired_cfg:
        paired_cfg = clone_le_device(
            adapter_config, adapter_mac, device_mac, dump_macs
        )
        if not paired_cfg:
            return

    require_update = False

    if "IRK" in adapter_config and "IdentityResolvingKey" in paired_cfg:
        irk = format_hex(adapter_config["IRK"])
        cur_irk = paired_cfg["IdentityResolvingKey"].get("Key", "")
        if print_update_values("IdentityResolvingKey", cur_irk, irk):
            paired_cfg["IdentityResolvingKey"]["Key"] = irk
            require_update = True

    if "CSRK" in adapter_config and "LocalSignatureKey" in paired_cfg:
        csrk = format_hex(adapter_config["CSRK"])
        cur_csrk = paired_cfg["LocalSignatureKey"].get("Key", "")
        if print_update_values("LocalSignatureKey", cur_csrk, csrk):
            paired_cfg["LocalSignatureKey"]["Key"] = csrk
            require_update = True

    if "LTK" in adapter_config:
        ltk = format_hex(adapter_config["LTK"])
        for s in ("LongTermKey", "SlaveLongTermKey", "PeripheralLongTermKey"):
            if s in paired_cfg:
                cur_ltk = paired_cfg[s].get("Key", "")
                if print_update_values(s, cur_ltk, ltk):
                    paired_cfg[s]["Key"] = ltk
                    require_update = True

    if "KeyLength" in adapter_config:
        key_len = str(int(format_dword(adapter_config["KeyLength"]), 16))
        for s in ("LongTermKey", "SlaveLongTermKey", "PeripheralLongTermKey"):
            if s in paired_cfg:
                cur_len = paired_cfg[s].get("EncSize", "")
                if print_update_values("  EncSize", cur_len, key_len):
                    paired_cfg[s]["EncSize"] = key_len
                    require_update = True

    if "EDIV" in adapter_config:
        ediv = str(int(format_dword(adapter_config["EDIV"]), 16))
        for s in ("LongTermKey", "SlaveLongTermKey", "PeripheralLongTermKey"):
            if s in paired_cfg:
                cur_ediv = paired_cfg[s].get("EDiv", "")
                if print_update_values("  EDiv", cur_ediv, ediv):
                    paired_cfg[s]["EDiv"] = ediv
                    require_update = True

    if "ERand" in adapter_config:
        rand = str(int(format_hex_b(adapter_config["ERand"]), 16))
        for s in ("LongTermKey", "SlaveLongTermKey", "PeripheralLongTermKey"):
            if s in paired_cfg:
                cur_rand = paired_cfg[s].get("Rand", "")
                if print_update_values("  Rand", cur_rand, rand):
                    paired_cfg[s]["Rand"] = rand
                    require_update = True

    if not require_update:
        return

    if input("    > Update keys for device? (y/N): ").strip().lower() == "y":
        update_system_pairing(adapter_mac, device_mac, paired_cfg)
        print("    > OK!")


def load_keys(contents):
    contents = contents.replace('"', "").replace("=", " = ")
    contents = re.sub(
        r"HKEY_LOCAL_MACHINE\\SYSTEM\\.*?\\Services\\BTHPORT\\Parameters\\Keys\\?",
        "",
        contents,
    )
    lines = contents.replace("\r\n", "\n").split("\n")
    cleaned = [
        line
        for line in lines
        if line.strip() != "[]"
        and not line.startswith("Windows Registry Editor")
        and not line.startswith(";")
    ]

    cfg = configparser.ConfigParser()
    cfg.read_string("\n".join(cleaned))
    return cfg


def process_devices(config):
    sections = sorted(config.sections())
    dump_macs = set()
    for sec in sections:
        if "\\" in sec:
            dump_macs.add(format_mac(sec.split("\\")[1]))
        else:
            for k in config[sec]:
                if k not in ("masterirk", "centralirk"):
                    dump_macs.add(format_mac(k))

    for sec in sections:
        if "\\" not in sec:
            adapter_mac = format_mac(sec)
            print(f"\nBluetooth Adapter - {adapter_mac}")
            process_basic_pairing(config[sec], adapter_mac)
        else:
            ad_mac, dev_mac = (format_mac(x) for x in sec.split("\\"))
            print(f"\nBluetooth Adapter - {ad_mac}")
            process_advanced_pairing(
                config[sec], ad_mac, dev_mac, dump_macs
            )


def main():
    if os.geteuid() != 0:
        print("[-] Must run with sudo.")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Sync Bluetooth Keys from Windows to Linux"
    )
    parser.add_argument(
        "-w",
        "--windows-dir",
        help="Path to Windows mount root (auto-detected if omitted)",
    )
    parser.add_argument("-r", "--reg-file", help="Path to exported .reg file")
    args = parser.parse_args()

    content = None
    if args.reg_file:
        with codecs.open(
            args.reg_file, "r", encoding="utf-16-le", errors="ignore"
        ) as f:
            content = f.read()
    elif args.windows_dir:
        content = export_registry(args.windows_dir)
    else:
        # Auto-detect Windows mount
        found = find_windows_mount()
        if len(found) == 1:
            print(f"[*] Auto-detected Windows at: {found[0]}")
            content = export_registry(found[0])
        elif len(found) > 1:
            print(f"[-] Multiple Windows mounts found: {found}")
            print("    Please specify which one using -w <path>")
            sys.exit(1)
        else:
            print(
                "[-] Could not auto-detect Windows. Ensure it is mounted or specify with -w <path>"
            )
            sys.exit(1)

    cfg = load_keys(content)
    process_devices(cfg)
    print("\n[+] Done! Run: sudo systemctl restart bluetooth")


if __name__ == "__main__":
    main()
