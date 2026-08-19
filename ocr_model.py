import torch
import torch.nn as nn
import torchvision.transforms as transforms
from PIL import Image

CHARS = "0123456789."
PAD_IDX = 11

def string_to_tensor(s, max_len):
    tensor = torch.full((max_len,), PAD_IDX, dtype=torch.long)
    s_str = str(s)
    # Right align: put characters at the end of the tensor to match physical LCD slots
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

import torchvision.models as models

class MeterCRNN(nn.Module):
    def __init__(self, freeze_backbone=False):
        super(MeterCRNN, self).__init__()
        
        # Load a pretrained ResNet34 backbone for more parameters
        resnet = models.resnet34(weights=models.ResNet34_Weights.DEFAULT)
        # Keep up to Layer 2 (downsamples by 8 instead of 16)
        self.backbone = nn.Sequential(*list(resnet.children())[:-4])
        
        if freeze_backbone:
            for param in self.backbone.parameters():
                param.requires_grad = False
                
        
        # Sequence Pooling: Flatten the height into channels to preserve vertical features
        # Backbone output is [B, 128, 8, 32] -> C * H = 128 * 8 = 1024
        
        # Bidirectional LSTM to scan across the 32 horizontal timesteps
        self.lstm = nn.LSTM(input_size=1024, hidden_size=128, num_layers=2, 
                            batch_first=True, bidirectional=True, dropout=0.3)
                            
        # Dropout for regularization
        self.dropout = nn.Dropout(0.3)
        
        # Linear layer mapping the LSTM hidden states to the 12 classes (10 digits + 1 decimal + 1 CTC Blank)
        # Bidirectional LSTM means hidden size is 128 * 2 = 256
        self.fc_cond = nn.Linear(256, 12)

    def forward(self, x):
        # Extract features
        x = self.backbone(x) # [batch_size, 128, 8, 32]
        
        # Flatten the height dimension into the channel dimension
        B, C, H, W = x.size()
        x = x.view(B, C * H, W) # [batch_size, 1024, 32]
        
        # Prepare for LSTM: [batch, channels, width] -> [batch, width, channels]
        x = x.permute(0, 2, 1) # [batch_size, 32, 1024]
        
        x = self.dropout(x)
        
        # Scan across image width
        lstm_out, _ = self.lstm(x) # [batch_size, 32, 256]
        
        # Produce logits for each timestep
        cond_out = self.fc_cond(lstm_out) # [batch_size, 32, 12]
        
        # CTC Loss in PyTorch expects shape: [sequence_length, batch_size, num_classes]
        cond_out = cond_out.permute(1, 0, 2)
        
        # CTC Loss requires Log Softmax
        return nn.functional.log_softmax(cond_out, dim=2)
