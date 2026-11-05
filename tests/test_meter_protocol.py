"""Decoding the Apera meter's serial packets (meter_protocol.py), checked against the layout Apera's
PC-Link software uses."""

import datetime
import struct

import pytest

import meter_protocol as mp

NOON = datetime.datetime(2026, 11, 4, 12, 0, 5)


def packet(mode=2, unit=0, decimals=3, digits=1234, temp_tenths=225, when=NOON, kind=b"DN"):
    """Build a packet like the meter sends (see the word layout in meter_protocol's docstring)."""
    w1 = mode * 4096 + when.minute * 64 + when.second
    w2 = (when.year - 2000) * 512 + when.month * 32 + when.day
    w3 = when.hour * 2048 + temp_tenths
    w4 = unit * 8192 + decimals * 2048 + digits
    return b"<" + kind + struct.pack(">HHHH", w1, w2, w3, w4) + b">"


def test_conductivity_in_ms():
    r = mp.parse_data_packet(packet(unit=0, decimals=3, digits=1234))
    assert r.is_conductivity and r.mode_name == "Cond"
    assert r.unit == "mS/cm"
    assert r.display == "1.234"
    assert r.cond_ms == pytest.approx(1.234)
    assert r.temp_c == pytest.approx(22.5)
    assert r.meter_time == NOON
    assert r.meter_time_text == "2026-11-04 12:00:05"
    assert not r.stored


def test_conductivity_in_us():
    r = mp.parse_data_packet(packet(unit=1, decimals=1, digits=123))
    assert r.unit == "uS/cm"
    assert r.display == "12.3"
    assert r.cond_ms == pytest.approx(0.0123)


def test_whole_number_display():
    r = mp.parse_data_packet(packet(unit=1, decimals=0, digits=530))
    assert r.display == "530"
    assert r.cond_ms == pytest.approx(0.530)


@pytest.mark.parametrize(("mode", "name"), [(0, "pH"), (1, "mV"), (3, "TDS"), (4, "Salinity"), (6, "DO")])
def test_other_modes_are_not_conductivity(mode, name):
    r = mp.parse_data_packet(packet(mode=mode, unit=0, decimals=1, digits=735))
    assert r.mode_name == name
    assert not r.is_conductivity
    assert r.unit is None and r.cond_ms is None  # never logged as conductivity


def test_unknown_conductivity_unit_has_no_value():
    r = mp.parse_data_packet(packet(unit=2, decimals=1, digits=100))
    assert r.is_conductivity and r.unit is None and r.cond_ms is None


def test_stored_reading_from_memory():
    assert mp.parse_data_packet(packet(kind=b"DS")).stored


def test_invalid_meter_date_is_kept_as_text():
    raw = bytearray(packet())
    raw[5:7] = struct.pack(">H", 26 * 512 + 15 * 32 + 4)  # month 15
    r = mp.parse_data_packet(bytes(raw))
    assert r.meter_time is None
    assert r.meter_time_text.startswith("2026-15-04")


def test_short_data_packet_is_an_error_and_other_packets_are_ignored():
    with pytest.raises(ValueError):
        mp.parse_data_packet(b"<DN\x00\x01>")
    assert mp.parse_data_packet(b"<SH12345678>") is None


def test_info_packets():
    assert mp.parse_info_packet(b"<SH" + bytes(range(8)) + b">") == ("SN", "00-01-02-03-04-05-06-07")
    assert mp.parse_info_packet(b"<P2" + bytes([0xAB] * 8) + b">")[0] == "Model"
    assert mp.parse_info_packet(packet()) is None


def test_split_packets_keeps_a_partial_packet():
    one, two = packet(), packet(digits=1)
    packets, rest = mp.split_packets(b"#\r\n" + one + two[:5])
    assert packets == [one]
    assert rest == two[:5]
    packets, rest = mp.split_packets(rest + two[5:])
    assert packets == [two] and rest == b""


def test_split_packets_caps_noise():
    _, rest = mp.split_packets(b"x" * 100_000)
    assert len(rest) == mp.MAX_BUFFER


def test_meter_reply_recognised():
    assert mp.is_meter_reply(b"#>")
    assert mp.is_meter_reply(b"junk<SH\x01\x02")
    assert not mp.is_meter_reply(b"OK\r\n")  # e.g. a pump or balance answering


def test_set_clock_command():
    cmd = mp.set_clock_command(datetime.datetime(2026, 11, 4, 13, 5, 9))
    assert cmd == b"&DT" + bytes([26, 11, 4, 13, 5, 9]) + b"#\r\n"


def test_clock_offset():
    assert mp.clock_offset_seconds(NOON, NOON + datetime.timedelta(minutes=2)) == -120
    assert mp.clock_offset_seconds(None, NOON) is None
    assert mp.describe_offset(-120) == "2 min behind"
    assert mp.describe_offset(6 * 365 * 86400) == "2190 days ahead"


@pytest.mark.parametrize(
    ("device", "description", "vid", "probe"),
    [
        ("COM5", "USB-SERIAL CH340 (COM5)", 0x1A86, True),
        ("COM1", "Communications Port (COM1)", None, False),
        ("COM7", "Standard Serial over Bluetooth link (COM7)", None, False),
        ("/dev/cu.usbserial-0001", "CP2102", 0x10C4, True),
        ("/dev/tty.usbserial-0001", "CP2102", 0x10C4, False),
        ("/dev/cu.debug-console", "n/a", None, False),
        ("/dev/ttyUSB0", "CP2102 USB to UART", 0x10C4, True),
    ],
)
def test_auto_detect_only_probes_usb_ports(device, description, vid, probe):
    assert mp.plausible_port(device, description, vid) is probe
