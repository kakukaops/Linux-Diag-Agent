"""MCE error code decoding (T-019).

Ref: Intel SDM Vol 3B Chapter 15 (Machine-Check Architecture)
     AMD APM Vol 2 Chapter 21
"""

from __future__ import annotations


# MCI_STATUS register bit positions
_BIT_VAL = 63   # valid
_BIT_OVER = 62  # overflow (more errors occurred)
_BIT_UC = 61    # uncorrected
_BIT_EN = 60    # enabled
_BIT_MISCV = 59 # MISC valid
_BIT_ADDRV = 58 # ADDR valid
_BIT_PCC = 57   # processor context corrupted (fatal)
_BIT_S = 56     # signaling
_BIT_AR = 55    # action required


# MCA error codes: bits 15:0 of MCI_STATUS
# Source: Intel SDM Vol 3B Table 15-9 + 15-12
_MCA_ERROR_CODES: dict[int, str] = {
    # Generic errors
    0x0001: "Unclassified error",
    0x0002: "Microcode ROM parity error",
    0x0003: "External error",
    0x0004: "FRC error",
    0x0005: "Internal parity error",

    # Memory hierarchy errors (TLB / Cache / BUS)
    # Pattern: 0b0000_0001_0TTT_LLLL
    0x0010: "L0 instruction cache error",
    0x0020: "L1 data cache error",
    0x0030: "L1 cache error",
    0x0100: "L0 TLB error",
    0x0110: "L1 TLB error",
    0x0120: "L2 TLB error",

    # Memory controller errors
    0x0080: "Generic memory error",
    0x0090: "Memory read error",
    0x00A0: "Memory write error",
    0x00C0: "Memory scrubbing error",

    # Bus / Interconnect errors (bits 11:4 = 1000 or 1010)
    0x0800: "Generic bus error",
    0x0900: "I/O transaction error",
    0x0A00: "PCIe bus error",

    # UC memory errors (common DIMM failure codes)
    0xC000: "Memory ECC uncorrected (channel 0)",
    0xC001: "Memory ECC uncorrected (channel 1)",
    0xC002: "Memory ECC uncorrected (channel 2)",
    0xC003: "Memory ECC uncorrected (channel 3)",

    # Corrected memory errors
    0x8000: "Memory ECC corrected",
}


def decode_mci_status(value: int) -> dict:
    """Decode a raw MCI_STATUS hex integer into structured fields.

    Args:
        value: integer from 'MCi_STATUS' field in mcelog output.

    Returns dict with: valid, uncorrected, fatal, overflow, pcc,
                       error_type (string), raw_error_code (hex).
    """
    if not (value & (1 << _BIT_VAL)):
        return {"valid": False, "uncorrected": False, "fatal": False,
                "error_type": "Invalid MCE record", "raw_error_code": hex(value)}

    error_code = value & 0xFFFF
    # Try exact match first, then masked match
    error_type = (
        _MCA_ERROR_CODES.get(error_code)
        or _MCA_ERROR_CODES.get(error_code & 0xFF00)
        or _MCA_ERROR_CODES.get(error_code & 0xF000)
        or f"Unknown MCA error 0x{error_code:04x}"
    )

    return {
        "valid": True,
        "overflow": bool(value & (1 << _BIT_OVER)),
        "uncorrected": bool(value & (1 << _BIT_UC)),
        "fatal": bool(value & (1 << _BIT_PCC)),
        "action_required": bool(value & (1 << _BIT_AR)),
        "error_type": error_type,
        "raw_error_code": hex(error_code),
        "severity": (
            "fatal" if value & (1 << _BIT_PCC)
            else "uncorrected" if value & (1 << _BIT_UC)
            else "corrected"
        ),
    }
