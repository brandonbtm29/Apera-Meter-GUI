"""The Apera EC400S / EC8500 serial protocol: commands, and decoding the meter's packets.

No Qt and no serial port here, so it can be tested on its own (tests/test_meter_protocol.py).

The meter talks at 115200 baud. Packets start with ``<`` and end with ``>``:

* ``<DN`` + 8 bytes + ``>``: a reading as it is measured. ``<DS`` is the same layout for a reading
  stored in the meter's memory (sent after ``&WA#``, Download Raw Memory).
* ``<SH`` + 8 bytes: the serial number; ``<P2`` + 8 bytes: the model.
* ``#>``: the meter's answer to ``&OK#``.

The 8 data bytes are four big-endian 16-bit words, decoded like Apera's own PC-Link software does
(its ``Recv_Data``):

* word 1: mode (``// 4096``), minute, second
* word 2: year - 2000, month, day
* word 3: hour, MTC flag, temperature in 0.1 °C
* word 4: unit code (``// 8192``), number of decimals, displayed digits

Mode 2 is conductivity, with unit code 0 = mS/cm and 1 = µS/cm (PC-Link: unit = code + 10).
"""

import datetime
import struct
from dataclasses import dataclass

BAUD = 115200
PING = b"&OK#\r\n"  # the meter answers with "#>" (and sends its serial number and model)
DOWNLOAD_MEMORY = b"&WA#\r\n"  # the meter sends its stored readings as <DS...> packets

MODES = {0: "pH", 1: "mV", 2: "Cond", 3: "TDS", 4: "Salinity", 5: "Resistivity", 6: "DO"}
COND_MODE = 2
COND_UNITS = {0: "mS/cm", 1: "uS/cm"}  # unit code (top 3 bits of word 4) in conductivity mode

# Prefixes that only an Apera meter sends; used to recognise the meter when probing a port.
_MAGIC = (b"<SH", b"<SL", b"<P2", b"<DN", b"<DS", b"#>")
MAX_BUFFER = 4096  # bytes kept while waiting for the end of a packet (a packet is 12 bytes)


@dataclass(frozen=True)
class Reading:
    """One decoded <DN>/<DS> packet."""

    stored: bool  # True for a reading from the meter's memory (<DS>)
    mode: int
    meter_time: datetime.datetime | None  # the meter's own clock; None if it isn't a valid date
    meter_time_text: str  # as sent, e.g. "2026-11-04 12:00:00" (kept even when it isn't a date)
    digits: int  # the number on the display, without its decimal point
    decimals: int
    unit_code: int
    temp_c: float
    raw_word4: int  # kept for the OCR training dataset (raw_cond column)

    @property
    def mode_name(self):
        return MODES.get(self.mode, f"Mode({self.mode})")

    @property
    def is_conductivity(self):
        return self.mode == COND_MODE

    @property
    def value(self):
        """The number on the display, e.g. 1.234."""
        return self.digits / (10**self.decimals)

    @property
    def display(self):
        """The number as the display shows it, e.g. "1.234" or "530"."""
        return f"{self.value:.{self.decimals}f}"

    @property
    def unit(self):
        """'mS/cm' or 'uS/cm' for a conductivity reading, else None."""
        if not self.is_conductivity:
            return None
        return COND_UNITS.get(self.unit_code)

    @property
    def cond_ms(self):
        """Conductivity in mS/cm, or None if this isn't a conductivity reading in mS or µS."""
        unit = self.unit
        if unit == "mS/cm":
            return self.value
        if unit == "uS/cm":
            return self.value / 1000.0
        return None


def parse_data_packet(packet):
    """Decode a <DN...> or <DS...> packet; returns a Reading, or None for any other packet.

    Raises ValueError for a data packet that is too short.
    """
    packet = packet.strip(b"\r\n")
    if not (packet.startswith(b"<DN") or packet.startswith(b"<DS")):
        return None
    if len(packet) < 12:
        raise ValueError(f"Data packet too short ({len(packet)} bytes)")
    w1, w2, w3, w4 = struct.unpack(">HHHH", packet[3:11])

    mode, rest = divmod(w1, 4096)
    minute, second = divmod(rest, 64)
    year, rest = divmod(w2, 512)
    month, day = divmod(rest, 32)
    hour, rest = divmod(w3, 2048)
    ntc = rest % 1024
    unit_code, rest = divmod(w4, 8192)
    decimals, digits = divmod(rest, 2048)

    text = f"20{year:02d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"
    try:
        meter_time = datetime.datetime(2000 + year, month, day, hour, minute, second)
    except ValueError:
        meter_time = None
    return Reading(
        stored=packet.startswith(b"<DS"),
        mode=mode,
        meter_time=meter_time,
        meter_time_text=text,
        digits=digits,
        decimals=decimals,
        unit_code=unit_code,
        temp_c=ntc / 10.0,
        raw_word4=w4,
    )


def parse_info_packet(packet):
    """('SN' or 'Model', hex text) for a <SH...> / <P2...> packet, else None."""
    packet = packet.strip(b"\r\n")
    if len(packet) < 12:
        return None
    kind = {b"<SH": "SN", b"<P2": "Model"}.get(packet[:3])
    if kind is None:
        return None
    return kind, "-".join(f"{b:02X}" for b in packet[3:11])


def split_packets(buffer):
    """Split received bytes into complete packets: returns (packets, bytes left over).

    The left-over part (an unfinished packet) is capped at MAX_BUFFER bytes, so noise without a
    ">" can't make it grow for ever.
    """
    buffer = buffer.replace(b"#\r\n", b"")
    packets = []
    while True:
        end = buffer.find(b">")
        if end < 0:
            break
        packets.append(buffer[: end + 1])
        buffer = buffer[end + 1 :]
    if len(buffer) > MAX_BUFFER:
        buffer = buffer[-MAX_BUFFER:]
    return packets, buffer


def is_meter_reply(data):
    """True if these bytes can only have come from an Apera meter."""
    return any(magic in data for magic in _MAGIC)


def set_clock_command(when):
    """The command that sets the meter's clock (what PC-Link's SyncTime sends)."""
    return (
        b"&DT"
        + bytes([when.year - 2000, when.month, when.day, when.hour, when.minute, when.second])
        + b"#\r\n"
    )


def clock_offset_seconds(meter_time, pc_time):
    """How far the meter's clock is ahead of the PC's (negative = behind); None if unknown."""
    if meter_time is None:
        return None
    return (meter_time - pc_time).total_seconds()


def describe_offset(seconds):
    """'3 min 20 s ahead', '2 days behind', ... for the clock warning."""
    ahead = "ahead" if seconds > 0 else "behind"
    s = abs(seconds)
    if s >= 2 * 86400:
        amount = f"{s / 86400:.0f} days"
    elif s >= 2 * 3600:
        amount = f"{s / 3600:.0f} h"
    elif s >= 120:
        amount = f"{s / 60:.0f} min"
    else:
        amount = f"{s:.0f} s"
    return f"{amount} {ahead}"


def plausible_port(device, description="", vid=None):
    """Whether auto-detect should try this port.

    The meter is a USB device, so built-in serial ports, Bluetooth ports and macOS's debug console
    are skipped: writing a probe to them is at best useless and at worst disturbs another device.
    """
    text = f"{device} {description}".lower()
    if "bluetooth" in text or "debug-console" in text or "wlan-debug" in text:
        return False
    if device.startswith("/dev/tty."):
        return False  # macOS lists each port twice; the /dev/cu.* twin is the one to use
    return vid is not None or "usb" in text
