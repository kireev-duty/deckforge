"""Конвертация единиц OOXML."""

EMU_PER_INCH = 914400
EMU_PER_PT = 12700
EMU_PER_CM = 360000
PX_PER_INCH = 96


def emu_to_pt(v: int) -> float:
    return v / EMU_PER_PT


def pt_to_emu(v: float) -> int:
    return round(v * EMU_PER_PT)


def emu_to_px(v: int, dpi: int = PX_PER_INCH) -> float:
    return v / EMU_PER_INCH * dpi


def px_to_emu(v: float, dpi: int = PX_PER_INCH) -> int:
    return round(v / dpi * EMU_PER_INCH)


def emu_to_in(v: int) -> float:
    return v / EMU_PER_INCH
