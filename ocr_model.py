import torch
import torch.nn as nn
import torchvision.transforms as transforms
from PIL import Image

CHARS = "0123456789."
PAD_IDX = 11

def string_to_tensor(s, max_len):
    tensor = torch.full((max_len,), PAD_IDX, dtype=torch.long)
    for i, char in enumerate(str(s)):
        if i >= max_len:
            break
        if char in CHARS:
            tensor[i] = CHARS.index(char)
    return tensor

def tensor_to_string(tensor):
    s = ""
    for idx in tensor:
        if idx.item() == PAD_IDX:
            continue
        if idx.item() < len(CHARS):
            s += CHARS[idx.item()]
    return s

class MeterCNN(nn.Module):
    def __init__(self):
        super(MeterCNN, self).__init__()
        # Input: 3x128x128
        self.features = nn.Sequential(
            nn.Conv2d(3, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2, 2), # 64x64
            
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2, 2), # 32x32
            
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2, 2), # 16x16
            
            nn.Conv2d(128, 256, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2, 2), # 8x8
        )
        
        self.fc_features = 256 * 8 * 8
        
        # Output layers for Cond (7 chars x 12 classes)
        self.fc_cond = nn.Linear(self.fc_features, 7 * 12)

    def forward(self, x):
        x = self.features(x)
        x = x.view(x.size(0), -1) # Flatten
        
        cond_out = self.fc_cond(x).view(-1, 7, 12) # [batch_size, 7, 12]
        
        return cond_out
