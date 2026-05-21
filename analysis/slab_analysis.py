"""
Slab Analysis Module for Balcony, Terrace, and Parking.
Extracts mathematically simplified polygons for BIM/Revit Floor elements.
"""
import cv2
import numpy as np

def extract_slab_polygon(mask, epsilon_factor=0.015):
    """
    Converts a binary mask into a simplified, closed polygon.
    Uses the Douglas-Peucker algorithm to reduce vertex count for Revit compatibility.
    
    Args:
        mask: Binary numpy array of the detected object (Balcony/Terrace/Parking)
        epsilon_factor: Controls polygon simplification (higher = fewer vertices, less accurate)
    
    Returns:
        List of [x, y] points forming a closed polygon.
    """
    # Ensure mask is uint8
    mask_uint8 = (mask * 255).astype(np.uint8)
    
    # 1. Find contours
    contours, _ = cv2.findContours(mask_uint8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not contours:
        return []
        
    # 2. Keep the largest contour (ignore noise/artifacts)
    largest_contour = max(contours, key=cv2.contourArea)
    
    # 3. Mathematical Simplification (Douglas-Peucker)
    # This prevents sending 5000 points to Revit for a simple rectangular balcony
    perimeter = cv2.arcLength(largest_contour, True)
    epsilon = epsilon_factor * perimeter
    simplified_polygon = cv2.approxPolyDP(largest_contour, epsilon, True)
    
    # 4. Format output as list of [x, y]
    points = []
    for p in simplified_polygon:
        x, y = float(p[0][0]), float(p[0][1])
        points.append([x, y])
        
    # Ensure polygon is closed for Revit Floor creation
    if len(points) > 2 and points[0] != points[-1]:
        points.append(points[0])
        
    return points