import cv2

for i in range(5):
    cap = cv2.VideoCapture(i)
    if not cap.isOpened():
        print(f"Camera {i} is not available")
        continue
    ret, frame = cap.read()
    if ret:
        print(f"Captured from camera {i} with shape {frame.shape}")
        cv2.imwrite(f"camera_{i}.jpg", frame)
    else:
        print(f"Failed to capture from camera {i}")
    cap.release()
