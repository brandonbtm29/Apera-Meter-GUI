import cv2
import numpy as np

def test_ocr(image_path):
    img = cv2.imread(image_path)
    if img is None:
        print("Image not found")
        return
        
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    
    # Wider range to catch glare
    lower_blue = np.array([75, 20, 20])
    upper_blue = np.array([135, 255, 255])
    
    mask = cv2.inRange(hsv, lower_blue, upper_blue)
    
    # Morphological operations to close gaps
    kernel = np.ones((5,5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    
    cv2.imwrite("mask.jpg", mask)
    
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    # Sort by area, get top 2
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:2]
    
    # Sort left to right
    def get_x(cnt):
        x, y, w, h = cv2.boundingRect(cnt)
        return x
    contours = sorted(contours, key=get_x)
    
    for i, cnt in enumerate(contours):
        epsilon = 0.02 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)
        
        if len(approx) == 4:
            pts = approx.reshape(4, 2)
            # Order points: top-left, top-right, bottom-right, bottom-left
            rect = np.zeros((4, 2), dtype="float32")
            s = pts.sum(axis=1)
            rect[0] = pts[np.argmin(s)]
            rect[2] = pts[np.argmax(s)]
            diff = np.diff(pts, axis=1)
            rect[1] = pts[np.argmin(diff)]
            rect[3] = pts[np.argmax(diff)]
            
            dst = np.array([
                [0, 0],
                [800 - 1, 0],
                [800 - 1, 600 - 1],
                [0, 600 - 1]], dtype="float32")
            
            M = cv2.getPerspectiveTransform(rect, dst)
            warped = cv2.warpPerspective(img, M, (800, 600))
            cv2.imwrite(f"warped_meter_{i+1}.jpg", warped)
            
        cv2.drawContours(img, [approx], -1, (0, 255, 0), 3)

    cv2.imwrite("contours.jpg", img)
    print("Done")

if __name__ == "__main__":
    test_ocr("camera_0.jpg")
