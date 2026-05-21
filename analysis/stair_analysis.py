"""
Stair Analysis Module.
Extracts rotated bounding boxes and footprint dimensions for BIM Stair objects.
"""
import cv2
import numpy as np

def extract_stair_footprint(mask):
    """
    Calculates the minimum enclosing rectangle for a staircase, accounting for rotation.
    
    Args:
        mask: Binary numpy array of the detected stair.
    
    Returns:
        Dictionary containing corner points, center, width, height, and angle.
    """
    mask_uint8 = (mask * 255).astype(np.uint8)
    
    # 1. Find contours
    contours, _ = cv2.findContours(mask_uint8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not contours:
        return None
        
    largest_contour = max(contours, key=cv2.contourArea)
    
    # 2. Calculate Minimum Area Rectangle (accounts for diagonal stairs)
    # Returns: (center(x, y), (width, height), angle of rotation)
    rect = cv2.minAreaRect(largest_contour)
    center, dimensions, angle = rect
    width, height = dimensions
    
    # 3. Get the 4 corners of the rotated rectangle
    box = cv2.boxPoints(rect)
    box = np.int32(box) # Convert to integer coordinates for safety. Replaced np.int0 to avoid NumPy 2.0 deprecation crash.
    
    # Format corners
    corners = [[float(p[0]), float(p[1])] for p in box]
    
    return {
        "center": [float(center[0]), float(center[1])],
        "dimensions": {
            "width": float(width),
            "length": float(height)
        },
        "rotation_angle": float(angle),
        "corners": corners
    }