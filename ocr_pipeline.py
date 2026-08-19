import cv2
import numpy as np
import threading
import time
import os
import json

try:
    import torch
    import torchvision.transforms as transforms
    from PIL import Image
    from ocr_model import MeterCRNN
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

class MeterOCR:
    def __init__(self, camera_index=0):
        self.camera_index = camera_index
        self.ocr_interval = 1.0
        self.cap = None
        self.is_running = False
        self.callbacks = []
        self.frame_callbacks = []
        self.last_warp_matrices = [None, None]
        self.rois = {
            "m1_cond": None,
            "m2_cond": None
        }
        self.use_midpoint_filter = True
        self.min_confidence = 0.0
        self.load_rois()
        
        self.model = None
        self.device = None
        self.transform = None
        if HAS_TORCH:
            torch.set_num_threads(1)  # Prevent PyTorch from using all CPU cores and starving the serial thread
            try:
                self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                self.model = MeterCRNN().to(self.device)
                if os.path.exists("meter_ocr_model.pt"):
                    self.model.load_state_dict(torch.load("meter_ocr_model.pt", map_location=self.device, weights_only=True))
                    self.model.eval()
                    self.transform = transforms.Compose([
                        transforms.Resize((128, 256)),
                        transforms.ToTensor(),
                        transforms.Normalize((0.5,), (0.5,))
                    ])
                    print("CNN Model loaded successfully.")
                else:
                    self.model = None
            except Exception as e:
                print(f"Failed to load CNN model: {e}")
                self.model = None

    def save_rois(self):
        try:
            with open("global_rois.json", "w") as f:
                json.dump(self.rois, f)
        except Exception as e:
            print(f"Error saving ROIs: {e}")

    def load_rois(self):
        try:
            if os.path.exists("global_rois.json"):
                with open("global_rois.json", "r") as f:
                    data = json.load(f)
                    for k in self.rois.keys():
                        if k in data and data[k] is not None:
                            roi = data[k]
                            # Make sure it has 5 elements
                            if len(roi) == 4:
                                roi.append(0.0)
                            self.rois[k] = roi
        except Exception as e:
            print(f"Error loading ROIs: {e}")
        
    def set_specific_roi(self, name, roi):
        if name in self.rois:
            self.rois[name] = roi
            
    def set_rois(self, r1, r2, r3, r4):
        self.rois = {
            "m1_cond": r1,
            "m2_cond": r3
        }

    def add_callback(self, cb):
        self.callbacks.append(cb)
        
    def add_frame_callback(self, cb):
        self.frame_callbacks.append(cb)

    def set_focus(self, val):
        if self.cap and self.cap.isOpened():
            if val < 0:
                self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
            else:
                self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
                self.cap.set(cv2.CAP_PROP_FOCUS, val)

    def start(self):
        if self.is_running: return
        import os
        if os.name == 'nt':
            self.cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
            if not self.cap.isOpened():
                self.cap = cv2.VideoCapture(self.camera_index)
        else:
            self.cap = cv2.VideoCapture(self.camera_index)
        # Set MJPG codec to allow USB bandwidth for 4K
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        # Try to set 4K resolution
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 3840)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 2160)
        
        self.is_running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.is_running = False
        if self.cap:
            self.cap.release()

    def _calculate_midpoint(self, vals, use_filter=True):
        if not vals: return (0.0, 0.0, 0.0)
        
        # Ensure all elements have length 3
        normalized_vals = []
        for v in vals:
            if len(v) == 2: normalized_vals.append((v[0], v[1], 0.0))
            else: normalized_vals.append(v)
            
        if len(normalized_vals) == 1: return normalized_vals[0]
        
        # If disabled, just return the most recent reading (the last one in the buffer)
        if not use_filter:
            return normalized_vals[-1]
            
        if len(normalized_vals) == 2: 
            return ((normalized_vals[0][0] + normalized_vals[1][0])/2.0, 
                    (normalized_vals[0][1] + normalized_vals[1][1])/2.0,
                    (normalized_vals[0][2] + normalized_vals[1][2])/2.0)
            
        def get_mid(idx):
            arr = [v[idx] for v in normalized_vals if v[idx] != 0.0]
            if len(arr) < 2: return arr[-1] if arr else 0.0
            
            min_diff = float('inf')
            best_pair = (arr[-1], arr[-1]) # Default to most recent
            for i in range(len(arr)):
                for j in range(i+1, len(arr)):
                    diff = abs(arr[i] - arr[j])
                    if diff < min_diff:
                        min_diff = diff
                        best_pair = (arr[i], arr[j])
            return (best_pair[0] + best_pair[1]) / 2.0
            
        def get_conf():
            confs = [v[2] for v in normalized_vals if v[0] != 0.0]
            if not confs: return 0.0
            return sum(confs) / len(confs)
            
        return (get_mid(0), get_mid(1), get_conf())

    def _loop(self):
        last_process = 0
        last_emit = 0
        oversample_buffer_m1 = []
        oversample_buffer_m2 = []
        
        while self.is_running:
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(0.05)
                continue
                
            display_frame = frame.copy()
            now = time.time()
            
            # Always draw ROIs so they stay on screen
            roi_imgs, roi_metadata = self.extract_crops(display_frame)
            if not hasattr(self, "last_parsed_vals"):
                self.last_parsed_vals = {}
                
            for name, meta in roi_metadata.items():
                color = (0, 0, 255) if "m1" in name else (255, 0, 0)
                if meta['box'] is not None:
                    cv2.drawContours(display_frame, [meta['box']], 0, color, 4)
                else:
                    cv2.rectangle(display_frame, (meta['x1'], meta['y1']), (meta['x1']+meta['w1'], meta['y1']+meta['h1']), color, 4)
                
                val_tuple = self.last_parsed_vals.get(name, (0.0, 0.0, 0.0))
                conf = 0.0
                if isinstance(val_tuple, tuple):
                    if len(val_tuple) == 3:
                        v_cnn, v_cv, conf = val_tuple
                    else:
                        v_cnn, v_cv = val_tuple
                    val = v_cnn if v_cnn != 0.0 else v_cv
                else:
                    val = val_tuple
                
                # Show exactly what the OCR read
                display_str = str(val)
                if display_str.endswith('.0'): 
                    display_str = display_str[:-2]
                    
                cv2.putText(display_frame, f"{name}: {display_str}", (meta['draw_x'], max(30, meta['draw_y']-10)), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
                
                # Draw measurement indicator
                last_inf = getattr(self, "last_inference_time", 0)
                if now - last_inf < 0.3: # Flash for 300ms
                    cv2.circle(display_frame, (max(10, meta['draw_x'] - 20), max(20, meta['draw_y'] - 15)), 10, (0, 255, 0), -1)
                
            if now - last_process >= (self.ocr_interval / 3.0):
                # Do inference on the un-drawn frame (3x oversampling)
                raw_roi_imgs = self.process_frame(frame)
                last_process = now
                self.last_inference_time = now
                
                m1 = self.last_parsed_vals.get("m1_cond", (0.0, 0.0))
                m2 = self.last_parsed_vals.get("m2_cond", (0.0, 0.0))
                oversample_buffer_m1.append(m1)
                oversample_buffer_m2.append(m2)
                
            if now - last_emit >= self.ocr_interval:
                if len(oversample_buffer_m1) > 0:
                    use_filter = getattr(self, 'use_midpoint_filter', True)
                    final_m1 = self._calculate_midpoint(oversample_buffer_m1, use_filter)
                    final_m2 = self._calculate_midpoint(oversample_buffer_m2, use_filter)
                    oversample_buffer_m1.clear()
                    oversample_buffer_m2.clear()
                    
                    readings = [final_m1, final_m2]
                    raw_roi_imgs, _ = self.extract_crops(frame)
                    
                    for cb in self.callbacks:
                        try:
                            import inspect
                            sig = inspect.signature(cb)
                            if len(sig.parameters) == 2:
                                cb(readings, raw_roi_imgs)
                            else:
                                cb(readings)
                        except: pass
                last_emit = now
                
            for cb in self.frame_callbacks:
                try: cb(display_frame.copy(), roi_imgs)
                except Exception as e:
                    pass
                
            time.sleep(0.1) # ~10 FPS limit
            
    def _order_points(self, pts):
        rect = np.zeros((4, 2), dtype="float32")
        s = pts.sum(axis=1)
        rect[0] = pts[np.argmin(s)] # top-left
        rect[2] = pts[np.argmax(s)] # bottom-right
        diff = np.diff(pts, axis=1)
        rect[1] = pts[np.argmin(diff)] # top-right
        rect[3] = pts[np.argmax(diff)] # bottom-left
        return rect

    def extract_crops(self, frame):
        fh, fw, _ = frame.shape
        roi_imgs = {}
        roi_metadata = {}
        
        for name, roi in self.rois.items():
            angle = 0.0
            if roi:
                norm_x, norm_y, norm_w, norm_h = roi[:4]
                if len(roi) > 4: angle = roi[4]
                x1, y1 = int(norm_x * fw), int(norm_y * fh)
                w1, h1 = int(norm_w * fw), int(norm_h * fh)
            else:
                if name == "m1_cond": x1, y1, w1, h1 = int(fw*0.1), int(fh*0.2), int(fw*0.35), int(fh*0.4)
                elif name == "m2_cond": x1, y1, w1, h1 = int(fw*0.55), int(fh*0.2), int(fw*0.35), int(fh*0.4)

            center = (x1 + w1 / 2.0, y1 + h1 / 2.0)
            
            if angle != 0.0:
                rect = ((center[0], center[1]), (w1, h1), angle)
                box = cv2.boxPoints(rect)
                box = np.int32(box)
                bx, by, bw, bh = cv2.boundingRect(box)
                bx_c, by_c = max(0, bx), max(0, by)
                crop_region = frame[by_c:min(fh, by+bh), bx_c:min(fw, bx+bw)]
                
                if crop_region.size > 0:
                    crop_center = (center[0] - bx_c, center[1] - by_c)
                    M = cv2.getRotationMatrix2D(crop_center, angle, 1.0)
                    rotated_crop = cv2.warpAffine(crop_region, M, (crop_region.shape[1], crop_region.shape[0]))
                    rx1 = int(crop_center[0] - w1/2)
                    ry1 = int(crop_center[1] - h1/2)
                    roi_img = rotated_crop[max(0, ry1):min(rotated_crop.shape[0], ry1+h1), max(0, rx1):min(rotated_crop.shape[1], rx1+w1)]
                else:
                    roi_img = np.array([])
                draw_x, draw_y = int(box[0][0]), int(box[0][1])
            else:
                roi_img = frame[max(0, y1):min(fh, y1+h1), max(0, x1):min(fw, x1+w1)]
                draw_x, draw_y = x1, y1
                box = None
                
            roi_imgs[name] = roi_img
            roi_metadata[name] = {'draw_x': draw_x, 'draw_y': draw_y, 'box': box, 'x1': x1, 'y1': y1, 'w1': w1, 'h1': h1}
            
        return roi_imgs, roi_metadata

    def _sanitize_for_display(self, ocr_val, last_serial_ms):
        try:
            if ocr_val == 0.0 or last_serial_ms == 0.0 or last_serial_ms is None:
                return float(ocr_val)
                
            raw_str = str(ocr_val).replace('.', '')
            raw_int = int(raw_str)
            if raw_int == 0: return 0.0
            raw_str = str(raw_int)
            
            possible_ms_values = []
            for i in range(len(raw_str) + 1):
                if i == 0:
                    s = "0." + raw_str
                elif i == len(raw_str):
                    s = raw_str
                else:
                    s = raw_str[:i] + "." + raw_str[i:]
                try:
                    possible_ms_values.append(float(s))
                except: pass
                
            best_val = min(possible_ms_values, key=lambda x: abs(x - last_serial_ms))
            
            if abs(best_val - last_serial_ms) / last_serial_ms > 0.5:
                return float(ocr_val)
                
            return float(best_val)
        except Exception:
            try: return float(ocr_val)
            except: return 0.0

    def process_frame(self, frame):
        fh, fw, _ = frame.shape
        readings = [None, None]
        parsed_vals = {}
        
        roi_imgs, roi_metadata = self.extract_crops(frame)
        
        for name, roi_img in roi_imgs.items():
            if roi_img.size == 0:
                parsed_vals[name] = (0.0, 0.0, 0.0)
                continue
                
            val_cv = self.parse_7segment(roi_img)
            val_cnn = 0.0
            conf_cnn = 0.0
            
            if self.model and self.transform:
                try:
                    val_cnn, conf_cnn = self.predict_cnn(roi_img)
                except Exception as e:
                    print(f"CNN Error: {e}")
                    val_cnn = val_cv
            else:
                val_cnn = val_cv
                
            parsed_vals[name] = (val_cnn, val_cv, conf_cnn)
            
        if not hasattr(self, "last_parsed_vals"):
            self.last_parsed_vals = {}
        self.last_parsed_vals.update(parsed_vals)
        
        return roi_imgs

    def predict_cnn(self, roi_img):
        if not HAS_TORCH or self.model is None or self.transform is None:
            return 0.0, 0.0
            
        # Convert BGR OpenCV image to PIL RGB
        rgb_img = cv2.cvtColor(roi_img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb_img)
        
        # Transform to tensor
        input_tensor = self.transform(pil_img).unsqueeze(0).to(self.device) # Add batch dim
        
        with torch.no_grad():
            output = self.model(input_tensor) # [16, 1, 11]
            
        # Get argmax over classes (dim=2) for the single batch item
        probs = torch.softmax(output[:, 0, :], dim=1) # [16, 12]
        preds = torch.argmax(probs, dim=1) # [16]
        
        # CTC Decode
        CHARS = "0123456789."
        PAD_IDX = 11
        pred_str = ""
        prev_char = -1
        char_probs = []
        for i, p in enumerate(preds):
            p = p.item()
            if p != PAD_IDX and p != prev_char:
                pred_str += CHARS[p]
                char_probs.append(probs[i, p].item())
            prev_char = p
            
        # Check confidence
        avg_conf = sum(char_probs) / len(char_probs) if char_probs else 0.0
        min_conf = getattr(self, 'min_confidence', 0.0)
        if min_conf > 0.0 and avg_conf < min_conf:
            return 0.0, avg_conf
        
        # Try converting to float
        try:
            # Remove any trailing invalid chars or extra dots
            pred_str = pred_str.strip()
            if not pred_str:
                return 0.0, avg_conf
            return float(pred_str), avg_conf
        except ValueError:
            return 0.0, avg_conf

    def parse_7segment(self, roi):
        # Crop the border
        roi = roi[5:-5, 5:-5]
        h, w = roi.shape[:2]
        if h == 0 or w == 0:
            return 0.0
            
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        
        # 1. CLAHE
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        gray = clahe.apply(gray)
        
        # 2. Blackhat for dark-on-light segments
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
        blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)
        
        # 3. Thresholding
        _, thresh = cv2.threshold(blackhat, 30, 255, cv2.THRESH_BINARY)
        
        # 4. Deskew
        shear_deg = 12
        shear_rad = shear_deg * np.pi / 180.0
        M = np.float32([
            [1, np.tan(shear_rad), -np.tan(shear_rad) * h],
            [0, 1, 0]
        ])
        deskewed = cv2.warpAffine(thresh, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        
        # 5. Remove horizontal lines (deskew artifacts)
        contours, _ = cv2.findContours(deskewed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        mask = np.zeros_like(deskewed)
        for c in contours:
            cx, cy, cw, ch = cv2.boundingRect(c)
            if ch > cw * 0.8:
                cv2.drawContours(mask, [c], -1, 255, -1)
        
        thresh = cv2.bitwise_and(deskewed, mask)
        
        # 6. Morphological closing to bridge gaps
        kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 5))
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel_close)
        
        # X-projection to find digits
        x_proj = np.sum(thresh, axis=0)
        x_proj_smooth = np.convolve(x_proj, np.ones(5)/5, mode='same')
        x_threshold = np.max(x_proj_smooth) * 0.1
        digit_cols = np.where(x_proj_smooth > x_threshold)[0]
        
        from itertools import groupby
        from operator import itemgetter
        regions = []
        for k, g in groupby(enumerate(digit_cols), lambda ix: ix[0] - ix[1]):
            group = list(map(itemgetter(1), g))
            if group[-1] - group[0] > 5: # minimum width of a digit (a '1' can be very thin)
                regions.append((group[0], group[-1]))
                
        if not regions:
            return 0.0
            
        # Global Y-projection to find top/bottom of text
        y_proj = np.sum(thresh, axis=1)
        y_proj_smooth = np.convolve(y_proj, np.ones(5)/5, mode='same')
        y_threshold = np.max(y_proj_smooth) * 0.1
        y_rows = np.where(y_proj_smooth > y_threshold)[0]
        if len(y_rows) == 0:
            return 0.0
        global_y1, global_y2 = y_rows[0], y_rows[-1]
        
        # Find the decimal point by looking at the gaps between digits
        max_gap_pixels = 0
        decimal_idx = -1
        for i, (x1, x2) in enumerate(regions):
            if i > 0:
                prev_x2 = regions[i-1][1]
                gap_roi = thresh[max(0, global_y2 - 30):global_y2, prev_x2:x1]
                if gap_roi.size > 0:
                    gap_pixels = np.sum(gap_roi)
                    if gap_pixels > max_gap_pixels and gap_pixels > 255 * 10:
                        max_gap_pixels = gap_pixels
                        decimal_idx = i
        
        result_str = ""
        for i, (x1, x2) in enumerate(regions):
            if i == decimal_idx:
                result_str += "."
                
            digit_roi = thresh[global_y1:global_y2, x1:x2]
            
            # Density check to reject edge artifacts
            density = np.sum(digit_roi) / (255.0 * digit_roi.shape[0] * digit_roi.shape[1])
            if density > 0.6:
                continue
                
            digit = self._identify_digit(digit_roi)
            if digit is not None:
                # If we parsed a 'C' or 'P', break (don't add to float)
                if isinstance(digit, str) and not digit.isdigit():
                    break
                result_str += str(digit)
                
        # Additional filtering: max 4 digits, first digit can only be 1
        digits = [c for c in result_str if c.isdigit()]
        decimal_offset = result_str.find('.')
        digits_before = 0
        if decimal_offset != -1:
            digits_before = len([c for c in result_str[:decimal_offset] if c.isdigit()])
            
        while len(digits) > 4:
            digits.pop(0)
            if decimal_offset != -1:
                digits_before = max(0, digits_before - 1)
                
        if len(digits) == 4 and digits[0] != '1':
            digits.pop(0)
            if decimal_offset != -1:
                digits_before = max(0, digits_before - 1)
                
        if not digits:
            return 0.0
            
        if decimal_offset != -1:
            digits.insert(digits_before, '.')
            
        result_str = "".join(digits)
        
        try:
            return float(result_str)
        except:
            return 0.0

    def _identify_digit(self, digit_mask):
        """Identify digit using projection-based segment detection."""
        h, w = digit_mask.shape
        
        # Horizontal bars (Top, Center, Bottom)
        mid_col = digit_mask[:, int(w*0.3):int(w*0.7)]
        y_proj = np.sum(mid_col, axis=1) # Shape: (h,)
        h_thresh = 0.5 * mid_col.shape[1] * 255
        
        top = np.max(y_proj[:int(h*0.3)]) > h_thresh if len(y_proj[:int(h*0.3)]) > 0 else False
        center = np.max(y_proj[int(h*0.3):int(h*0.7)]) > h_thresh if len(y_proj[int(h*0.3):int(h*0.7)]) > 0 else False
        bottom = np.max(y_proj[int(h*0.7):]) > h_thresh if len(y_proj[int(h*0.7):]) > 0 else False
        
        # Vertical bars (Top-Left, Bottom-Left, Top-Right, Bottom-Right)
        # Exclude the top/bottom 15% and center 20% to avoid overlap with horizontal bars
        q_tl = digit_mask[int(h*0.15):int(h*0.4), :int(w*0.4)]
        q_bl = digit_mask[int(h*0.6):int(h*0.85), :int(w*0.4)]
        q_tr = digit_mask[int(h*0.15):int(h*0.4), int(w*0.6):]
        q_br = digit_mask[int(h*0.6):int(h*0.85), int(w*0.6):]
        
        def has_vertical_bar(quad):
            if quad.size == 0:
                return False
            coords = cv2.findNonZero(quad)
            if coords is not None:
                x, y, w_rect, h_rect = cv2.boundingRect(coords)
                if h_rect > quad.shape[0] * 0.5 and h_rect >= w_rect:
                    return True
                if h_rect > quad.shape[0] * 0.7:
                    return True
            return False

        top_left = has_vertical_bar(q_tl)
        bottom_left = has_vertical_bar(q_bl)
        top_right = has_vertical_bar(q_tr)
        bottom_right = has_vertical_bar(q_br)
        
        on = (int(top), int(top_left), int(top_right), int(center), int(bottom_left), int(bottom_right), int(bottom))
        
        lookup = {
            (1, 1, 1, 0, 1, 1, 1): '0',
            (0, 0, 1, 0, 0, 1, 0): '1',
            (1, 0, 1, 1, 1, 0, 1): '2',
            (1, 0, 1, 1, 0, 1, 1): '3',
            (0, 1, 1, 1, 0, 1, 0): '4',
            (1, 1, 0, 1, 0, 1, 1): '5',
            (1, 1, 0, 1, 1, 1, 1): '6',
            (1, 0, 1, 0, 0, 1, 0): '7',
            (1, 1, 1, 1, 1, 1, 1): '8',
            (1, 1, 1, 1, 0, 1, 1): '9',
            (1, 1, 0, 0, 1, 0, 1): 'C',
            (1, 1, 1, 1, 1, 0, 0): 'P'
        }
        
        return lookup.get(on, None)

if __name__ == "__main__":
    ocr = MeterOCR()
    img = cv2.imread("camera_0.jpg")
    
    def my_cb(readings):
        print("Readings:", readings)
        
    ocr.add_callback(my_cb)
    ocr.process_frame(img)
