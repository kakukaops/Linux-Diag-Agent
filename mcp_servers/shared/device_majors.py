"""Linux device major number → driver/subsystem mapping.

Source: Documentation/admin-guide/devices.txt in mainline. Static entries are
the LANANA-registered numbers. Dynamic majors (240-254 for block, 234-254 for
char) are listed with their *typical* assignments on modern (≥5.x) kernels.

Used by:
  - agent/react/tools/code_tools._get_device_major_mapping (P1c v2.1)
  - Helps agent answer "major 254 is which driver?" when reading dmesg.
"""

from __future__ import annotations

# (major, kind, name)  kind ∈ {"block", "char"}
# When dynamic, "name" gives the typical assignment on modern distros (≥5.x).
_MAJORS: dict[tuple[int, str], dict] = {
    # ─── Block majors (mostly static below 240) ───
    (1, "block"):   {"driver": "ramdisk", "notes": "/dev/ram*"},
    (2, "block"):   {"driver": "fd (floppy)", "notes": "obsolete"},
    (3, "block"):   {"driver": "ide0 / pata", "notes": "legacy IDE master/slave"},
    (7, "block"):   {"driver": "loop", "notes": "/dev/loop*"},
    (8, "block"):   {"driver": "sd (SCSI/SATA/USB disk)",
                     "notes": "sd[a-z]: first 16 disks; 65-71 extends"},
    (9, "block"):   {"driver": "md (software RAID)", "notes": "/dev/md*"},
    (11, "block"):  {"driver": "sr (SCSI CD-ROM)", "notes": "/dev/sr*"},
    (22, "block"):  {"driver": "ide1", "notes": "legacy IDE 2nd controller"},
    (43, "block"):  {"driver": "nbd (network block device)", "notes": "/dev/nbd*"},
    (65, "block"):  {"driver": "sd (extended)", "notes": "sd disks 17-32"},
    (66, "block"):  {"driver": "sd (extended)", "notes": "sd disks 33-48"},
    (67, "block"):  {"driver": "sd (extended)", "notes": "sd disks 49-64"},
    (68, "block"):  {"driver": "sd (extended)", "notes": "sd disks 65-80"},
    (69, "block"):  {"driver": "sd (extended)", "notes": "sd disks 81-96"},
    (70, "block"):  {"driver": "sd (extended)", "notes": "sd disks 97-112"},
    (71, "block"):  {"driver": "sd (extended)", "notes": "sd disks 113-128"},
    (180, "block"): {"driver": "ub (USB block)", "notes": "deprecated; usually sd now"},
    (202, "block"): {"driver": "xvd (Xen virtual)", "notes": "/dev/xvd*"},
    (240, "block"): {"driver": "(dynamic)",
                     "notes": "Typically dm-* (device-mapper) on some distros"},
    (251, "block"): {"driver": "(dynamic)", "notes": "Typically zram or md"},
    (252, "block"): {"driver": "(dynamic, often virtio_blk OR nvme)",
                     "notes": "vd[a-z] on KVM/virtio; or nvme on some distros"},
    (253, "block"): {"driver": "(dynamic, often md or nbd)", "notes": ""},
    (254, "block"): {"driver": "(dynamic, MOST COMMONLY device-mapper / dm-*)",
                     "notes": "dm-0 / dm-1 (LVM, dm-crypt, multipath); "
                              "occasionally md or virtio_blk depending on load order"},
    (259, "block"): {"driver": "nvme (BLOCK_EXT_MAJOR)",
                     "notes": "/dev/nvme0n1 etc.; partition extension"},

    # ─── Char majors (selected, kernel-fault-relevant) ───
    (1, "char"):    {"driver": "mem", "notes": "/dev/null /dev/zero /dev/mem etc."},
    (4, "char"):    {"driver": "tty / serial", "notes": "/dev/tty*"},
    (5, "char"):    {"driver": "tty alts", "notes": "/dev/console /dev/ptmx"},
    (10, "char"):   {"driver": "misc", "notes": "watchdog, kvm, fuse via misc minor"},
    (13, "char"):   {"driver": "input", "notes": "/dev/input/*"},
    (29, "char"):   {"driver": "fb", "notes": "/dev/fb*"},
    (89, "char"):   {"driver": "i2c", "notes": "/dev/i2c-*"},
    (180, "char"):  {"driver": "usb", "notes": "/dev/usbdev*"},
    (188, "char"):  {"driver": "ttyUSB", "notes": "/dev/ttyUSB*"},
    (245, "char"):  {"driver": "(dynamic)", "notes": "often nvme-controller"},
    (246, "char"):  {"driver": "(dynamic)", "notes": "often hwmon or thermal"},
    (247, "char"):  {"driver": "(dynamic)", "notes": "often raw / dax"},
}


def lookup_major(major: int, kind: str = "block") -> dict:
    """Return {driver, notes, dynamic} for the given major number.

    `kind` ∈ {"block", "char"}; defaults to "block" (most dmesg refs).
    Returns {"driver": "unknown", ...} if not in the table.
    """
    entry = _MAJORS.get((major, kind))
    if entry:
        return {
            "major": major,
            "kind": kind,
            "driver": entry["driver"],
            "notes": entry["notes"],
            "dynamic": "(dynamic" in entry["driver"],
        }
    return {
        "major": major,
        "kind": kind,
        "driver": "unknown",
        "notes": (f"Major {major} {kind} is not in the static LANANA table. "
                  f"On a live system, check /proc/devices. "
                  f"Range 240-254 ({kind}) is dynamic allocation."),
        "dynamic": 240 <= major <= 254,
    }
