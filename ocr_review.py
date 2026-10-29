"""Flag live OCR readings that look wrong and save their camera snapshots for review.

While "Auto-Record OCR Training Dataset" is on, every OCR reading the app logs is checked against
the recent OCR readings and the meter's own serial reading. Suspicious ones get a snapshot in the
current dataset session, so the Dataset Curation tab can show them for a label check before they
go into the training set. The model's mistakes are the most useful training examples.

Flags (a reading can have several):
  magnitude        about 10x / 100x / 1000x off the median of the recent readings (decimal point,
                   uS/mS range, or a collapsed repeated digit such as 166.2 read as 16.x)
  jump             more than 25 % off that median (and not a magnitude error)
  serial_mismatch  more than 5 % off the meter's serial reading received in the last 5 s
  no_read          the OCR returned nothing while recent readings were non-zero
  low_conf         OCR confidence below 90 %
No Qt here, so it can be tested headless.
"""

import collections
import csv
import math
import os
import statistics

# review.csv sits next to readings.csv in Sessions/dataset_session_*/
REVIEW_CSV_HEADER = [
    "timestamp",
    "meter_id",
    "ocr_display",
    "ocr_logged_ms",
    "ocr_conf",
    "flags",
    "serial_display",
    "serial_age_s",
    "local_median_ms",
    "crop_file",
]


class OcrAnomalyDetector:
    """Per-meter check of logged OCR values (in mS/cm), with a snapshot rate limit."""

    def __init__(self, window=10, min_interval_s=15.0, max_snapshots=500, accept_after=5):
        self.history = collections.deque(maxlen=window)
        self.min_interval_s = min_interval_s
        self.max_snapshots = max_snapshots
        self.accept_after = accept_after  # a new level that persists this long is real, not an error
        self.last_snapshot = -math.inf
        self.snapshots = 0
        self._outlier_run = []
        self.median = None

    def check(self, value_ms, conf, serial_ms=None, serial_age_s=None):
        flags = []
        self.median = statistics.median(self.history) if len(self.history) >= 3 else None
        if conf is not None and conf < 0.90:
            flags.append("low_conf")
        outlier = False
        if self.median and self.median > 0:
            if not value_ms or value_ms <= 0:
                flags.append("no_read")
            else:
                ratio = value_ms / self.median
                if abs(math.log10(ratio)) >= 0.8:
                    flags.append("magnitude")
                    outlier = True
                elif abs(ratio - 1) > 0.25:
                    flags.append("jump")
                    outlier = True
        if serial_ms and serial_age_s is not None and serial_age_s <= 5 and value_ms and value_ms > 0:
            if abs(value_ms - serial_ms) / serial_ms > 0.05:
                flags.append("serial_mismatch")

        if value_ms and value_ms > 0:
            if outlier:
                self._outlier_run.append(value_ms)
                if len(self._outlier_run) >= self.accept_after:
                    # The reading has stayed at the new level: treat it as a real change.
                    self.history.clear()
                    self.history.extend(self._outlier_run)
                    self._outlier_run = []
            else:
                self._outlier_run = []
                self.history.append(value_ms)
        return flags

    def should_snapshot(self, flags, now):
        if not flags or self.snapshots >= self.max_snapshots:
            return False
        if now - self.last_snapshot < self.min_interval_s:
            return False
        self.last_snapshot = now
        self.snapshots += 1
        return True


def save_review_snapshot(session_dir, roi_img, meter_id, now, label, row):
    """Write the crop (128x128 jpg + native png) and a review.csv row. Returns the crop file name.

    The file name follows the curation tab's convention {label}_{serial_ts}_{frame_ts}_{meter}.jpg;
    label is "Unknown" unless the meter's serial reading pins down what the display showed.
    """
    import cv2

    if roi_img is None or getattr(roi_img, "size", 0) == 0:
        return ""
    crop_dir = os.path.join(session_dir, "extracted_crops")
    native_dir = os.path.join(session_dir, "extracted_crops_native")
    os.makedirs(crop_dir, exist_ok=True)
    os.makedirs(native_dir, exist_ok=True)
    filename = f"{label}_{now:.3f}_{now:.3f}_{meter_id}.jpg"
    cv2.imwrite(os.path.join(crop_dir, filename), cv2.resize(roi_img, (128, 128)))
    cv2.imwrite(os.path.join(native_dir, os.path.splitext(filename)[0] + ".png"), roi_img)

    path = os.path.join(session_dir, "review.csv")
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_CSV_HEADER)
        if new_file:
            w.writeheader()
        w.writerow({**row, "timestamp": f"{now:.3f}", "meter_id": meter_id, "crop_file": filename})
    return filename


def capture_key(filename):
    """The part of a crop's file name that survives relabelling: '{serial_ts}_{frame_ts}_{meter}'."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    parts = stem.split("_")
    return "_".join(parts[1:]) if len(parts) >= 4 else stem


def same_reading(a, b):
    """True if two display strings show the same number ('16.20' == '16.2'); False if either is missing."""
    if not a or not b or a == "Unknown" or b == "Unknown":
        return False
    try:
        return abs(float(a) - float(b)) < 1e-9
    except ValueError:
        return a == b
