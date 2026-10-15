import torch
import torch.nn as nn
import torchvision.models as models

CHARS = "0123456789."
PAD_IDX = 11


def string_to_tensor(s, max_len):
    tensor = torch.full((max_len,), PAD_IDX, dtype=torch.long)
    s_str = str(s)
    start_idx = max_len - len(s_str)
    for i, char in enumerate(s_str):
        idx = start_idx + i
        if idx >= max_len or idx < 0:
            continue
        if char in CHARS:
            tensor[idx] = CHARS.index(char)
    return tensor


def tensor_to_string(tensor):
    s = ""
    for idx in tensor:
        if idx.item() == PAD_IDX:
            continue
        if idx.item() < len(CHARS):
            s += CHARS[idx.item()]
    return s


class MeterCRNN(nn.Module):
    def __init__(self, freeze_backbone=True):
        super().__init__()

        # Winning Architecture: ResNet-34, Heavy Downsampling, 0.3 Dropout (Phase 3 Champion)
        resnet = models.resnet34(weights=None)

        # Heavy Downsampling
        self.backbone = nn.Sequential(*list(resnet.children())[:-2])

        if freeze_backbone:
            for param in self.backbone.parameters():
                param.requires_grad = False

        # 512 channels * 2 height = 1024 input features per horizontal slice
        self.lstm = nn.LSTM(
            input_size=1024, hidden_size=128, num_layers=2, batch_first=True, bidirectional=True, dropout=0.3
        )

        self.dropout = nn.Dropout(0.3)
        self.fc = nn.Linear(256, 12)

    def forward(self, x):
        x = self.backbone(x)

        B, C, H, W = x.size()
        x = x.view(B, C * H, W)
        x = x.permute(0, 2, 1).contiguous()

        x = self.dropout(x)

        orig_device = x.device
        if orig_device.type == "privateuseone":
            self.lstm = self.lstm.cpu()
            x = x.cpu()

        lstm_out, _ = self.lstm(x)

        if orig_device.type == "privateuseone":
            lstm_out = lstm_out.to(orig_device)
            self.fc = self.fc.to(orig_device)

        cond_out = self.fc(lstm_out)
        cond_out = cond_out.permute(1, 0, 2)
        return nn.functional.log_softmax(cond_out, dim=2)
