import os
import csv
import glob
import sys

SCREEN_LAG_SECONDS = 0.300

def get_closest_frame(target_timestamp, frames_dir, frame_files):
    """Finds the frame whose timestamp is closest to target_timestamp"""
    if not frame_files:
        return None
        
    closest_frame = None
    min_diff = float('inf')
    
    for f in frame_files:
        basename = os.path.basename(f)
        try:
            frame_ts = float(basename.replace(".jpg", ""))
            diff = abs(frame_ts - target_timestamp)
            if diff < min_diff:
                min_diff = diff
                closest_frame = f
        except ValueError:
            continue
            
    if min_diff < 0.5:
        return closest_frame
    return None

def main(session_dir):
    frames_dir = os.path.join(session_dir, "frames")
    csv_file = os.path.join(session_dir, "readings.csv")
    
    if not os.path.exists(frames_dir) or not os.path.exists(csv_file):
        print("Missing frames or csv")
        sys.exit(1)
        
    frame_files = glob.glob(os.path.join(frames_dir, "*.jpg"))
    print(f"Total frames initially: {len(frame_files)}")
    
    keep_frames = set()
    
    with open(csv_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                serial_ts = float(row["timestamp"])
            except ValueError:
                continue
                
            target_ts = serial_ts + SCREEN_LAG_SECONDS
            closest_frame = get_closest_frame(target_ts, frames_dir, frame_files)
            if closest_frame:
                keep_frames.add(closest_frame)
                
    print(f"Frames to keep: {len(keep_frames)}")
    
    # Now delete the rest
    deleted = 0
    for f in frame_files:
        if f not in keep_frames:
            try:
                os.remove(f)
                deleted += 1
            except Exception as e:
                print(f"Error deleting {f}: {e}")
                
    print(f"Deleted {deleted} frames. Space freed!")
    
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python cleanup_frames.py <session_dir>")
    else:
        main(sys.argv[1])
