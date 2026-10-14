"""The OCR network builds and returns one score per character class for each image slice."""

import pytest

torch = pytest.importorskip("torch", reason="OCR extra not installed (pip install -e '.[ocr]')")


def test_crnn_output_shape():
    from ocr_model import CHARS, MeterCRNN

    model = MeterCRNN().eval()
    with torch.no_grad():
        out = model(torch.zeros(2, 3, 64, 256))  # batch of 2, the size ocr_pipeline resizes to

    seq_len, batch, classes = out.shape
    assert batch == 2
    assert seq_len > 0
    assert classes == 12 and classes >= len(CHARS) + 1  # digits, '.', plus the CTC blank
