"""
Advanced Architectural OCR Detector using PaddleOCR and Fuzzy Matching.
Extracts room names and binds them to spatial coordinates for BIM integration.
"""

import cv2
import numpy as np
import logging
from paddleocr import PaddleOCR
from thefuzz import process

logger = logging.getLogger(__name__)

# Complete list of building spaces according to Chapter 4 of National Building Regulations
# Suitable for OCR + fuzzy matching
# Includes original Farsi, standard English, and common OCR variants
BUILDING_SPACES = [
    # ==================== Accommodation Spaces ====================
    {"farsi": "اتاق خواب", "english": "Bedroom", "category": "Accommodation", "variants": ["اتاق‌خواب", "bedroom", "8EDROOM", "اتاق خواب", "خواب", "BED ROOM"]},
    {"farsi": "نشیمن", "english": "Living Room", "category": "Accommodation", "variants": ["نشیمن خانوادگی", "livingroom", "نشیمن", "LIVING ROOM", "نشیم"]},
    {"farsi": "پذیرایی", "english": "Reception", "category": "Accommodation", "variants": ["پذیرایی", "salon", "reception", "8AZIRAI", "پذیرایی مهمان"]},
    {"farsi": "اتاق نشیمن", "english": "Sitting Room", "category": "Accommodation", "variants": ["اتاق نشیمن", "sittingroom"]},
    {"farsi": "اتاق مهمان", "english": "Guest Room", "category": "Accommodation", "variants": ["اتاق مهمان", "guestroom", "اتاق گوست"]},
    {"farsi": "اتاق مطالعه", "english": "Study Room", "category": "Accommodation", "variants": ["اتاق مطالعه", "study", "اتاق کار"]},

    # ==================== Cooking and Dining Spaces ====================
    {"farsi": "آشپزخانه", "english": "Kitchen", "category": "Cooking", "variants": ["آشپزخانه", "kitchen", "8SHPAZKHANEH", "اشپزخانه", "آشپز"]},
    {"farsi": "ناهارخوری", "english": "Dining Room", "category": "Cooking", "variants": ["ناهارخوری", "diningroom", "ناهار خوری", "DINING ROOM"]},
    {"farsi": "آشپزخانه باز", "english": "Open Kitchen", "category": "Cooking", "variants": ["آشپزخانه اپن", "open kitchen", "اپن"]},

    # ==================== Sanitary Spaces ====================
    {"farsi": "حمام", "english": "Bathroom", "category": "Sanitary", "variants": ["حمام", "bathroom", "8ATHROOM", "BATH", "بهام", "حمامک"]},
    {"farsi": "سرویس بهداشتی", "english": "Toilet", "category": "Sanitary", "variants": ["WC", "توالت", "سرویس", "sErvis", "توالت فرنگی", "W.C", "دستشویی"]},
    {"farsi": "دستشویی", "english": "Washroom", "category": "Sanitary", "variants": ["دستشویی", "lavatory", "sink room"]},
    {"farsi": "اتاق تعویض لباس", "english": "Changing Room", "category": "Sanitary", "variants": ["اتاق تعویض", "dressing room"]},

    # ==================== Access Spaces ====================
    {"farsi": "ورودی", "english": "Entrance", "category": "Access", "variants": ["ورودی", "entrance", "lobby", "لابی", "هال ورودی"]},
    {"farsi": "راهرو", "english": "Corridor", "category": "Access", "variants": ["راهرو", "corridor", "hallway", "دالان", "راهرو اصلی"]},
    {"farsi": "پله", "english": "Staircase", "category": "Access", "variants": ["پله", "stairs", "راه‌پله", "stair", "راه پله"]},
    {"farsi": "آسانسور", "english": "Elevator", "category": "Access", "variants": ["آسانسور", "lift", "elevator"]},
    {"farsi": "لابی", "english": "Lobby", "category": "Access", "variants": ["لابی", "lobby"]},

    # ==================== Semi-open and Open Spaces ====================
    {"farsi": "بالکن", "english": "Balcony", "category": "Semi-open", "variants": ["بالکن", "balcony", "balkon", "بالکن باز"]},
    {"farsi": "تراس", "english": "Terrace", "category": "Semi-open", "variants": ["تراس", "terrace", "تراس باز", "تراس روباز"]},
    {"farsi": "ایوان", "english": "Veranda", "category": "Semi-open", "variants": ["ایوان", "veranda", "porch", "ایوانک"]},
    {"farsi": "حیاط", "english": "Yard", "category": "Open", "variants": ["حیاط", "yard", "courtyard", "حیاط خلوت", "حیاط جلو"]},

    # ==================== Parking and Storage ====================
    {"farsi": "پارکینگ", "english": "Parking", "category": "Parking", "variants": ["پارکینگ", "parking", "8ARKING", "جای پارک", "پارکینگ روباز"]},
    {"farsi": "گاراژ", "english": "Garage", "category": "Parking", "variants": ["گاراژ", "garage"]},
    {"farsi": "انباری", "english": "Storage", "category": "Storage", "variants": ["انباری", "storage", "انبار", "warehouse", "انباری لباس"]},

    # ==================== Other Common Spaces ====================
    {"farsi": "اتاق کار", "english": "Office", "category": "Other", "variants": ["دفتر", "office", "اتاق کار", "اتاق اداری"]},
    {"farsi": "اتاق سرور", "english": "Server Room", "category": "Other", "variants": ["اتاق سرور", "server room"]},
    {"farsi": "موتورخانه", "english": "Mechanical Room", "category": "Other", "variants": ["موتورخانه", "boiler room"]},
    {"farsi": "اتاق تأسیسات", "english": "Utility Room", "category": "Other", "variants": ["اتاق تأسیسات", "utility room"]},
    {"farsi": "نورگیر", "english": "Lightwell", "category": "Other", "variants": ["نورگیر", "پاسیو", "lightwell", "اتریوم"]},
    {"farsi": "روف گاردن", "english": "Roof Garden", "category": "Other", "variants": ["روف گاردن", "roof garden", "فضای سبز پشت بام"]}
]

class SpatialOCRDetector:
    def __init__(self, lang='fa'): # Changed to 'fa' to support Farsi and numeric parsing
        # use_angle_cls=True allows reading rotated text (crucial for floor plans)
        logger.info("Initializing PaddleOCR Engine...")
        self.ocr_engine = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)
        self.min_confidence = 0.6
        self.fuzzy_threshold = 80  # Minimum similarity score for word correction (out of 100)
        
        # Flatten the dictionary for optimized fuzzy matching
        self.variant_to_standard = {}
        self.all_variants = []
        
        for space in BUILDING_SPACES:
            std_eng = space["english"].upper()
            std_fa = space["farsi"]
            cat = space["category"]
            
            # Combine all possible valid representations into one list
            items_to_add = [std_eng, std_fa] + [v.upper() for v in space["variants"]] + [v for v in space["variants"]]
            
            for item in items_to_add:
                item_upper = item.upper()
                if item_upper not in self.variant_to_standard:
                    self.variant_to_standard[item_upper] = {
                        "english": space["english"], 
                        "farsi": space["farsi"], 
                        "category": space["category"]
                    }
                    self.all_variants.append(item_upper)
                    
        self.all_variants = list(set(self.all_variants))

    def _get_center_point(self, bbox):
        """Calculate the geometric center of the text bounding box for room binding."""
        points = np.array(bbox)
        center_x = np.mean(points[:, 0])
        center_y = np.mean(points[:, 1])
        return [float(center_x), float(center_y)]

    def _standardize_room_name(self, raw_text):
        """Correct OCR errors by finding the closest match in the flattened architectural dictionary."""
        text_upper = raw_text.upper().strip()
        
        # Discard very short text (likely noise)
        if len(text_upper) < 2:
            return None
            
        best_match, score = process.extractOne(text_upper, self.all_variants)
        
        if score >= self.fuzzy_threshold:
            # Return the rich dictionary object mapped to this variant
            return self.variant_to_standard[best_match]
        
        # If no match is found above threshold, return original as unclassified
        return {"english": text_upper, "farsi": raw_text, "category": "Unclassified"}

    def detect_space_names(self, image_path_or_array):
        """
        Run OCR on the floor plan and extract spatial labels (Room Tags).
        """
        try:
            # Execute PaddleOCR
            result = self.ocr_engine.ocr(image_path_or_array, cls=True)
            
            detected_spaces = []
            
            # PaddleOCR returns a list within a list
            if result is None or len(result) == 0 or result[0] is None:
                return []

            for line in result[0]:
                bbox = line[0]  # Four-corner coordinates of the text
                raw_text = line[1][0]
                confidence = float(line[1][1])
                
                if confidence < self.min_confidence:
                    continue
                    
                standard_data = self._standardize_room_name(raw_text)
                if not standard_data:
                    continue
                    
                center_point = self._get_center_point(bbox)
                
                detected_spaces.append({
                    "id": f"RoomTag_{len(detected_spaces)+1}",
                    "name": standard_data["english"],          # Standard English for Revit
                    "local_name": standard_data["farsi"],      # Farsi name for local UI
                    "category": standard_data["category"],     # Space Category
                    "original_text": raw_text,
                    "confidence": confidence,
                    "insertion_point": center_point,           # X,Y coordinates for Revit insertion
                    "bbox": bbox
                })
                
            return detected_spaces
            
        except Exception as e:
            logger.error(f"OCR Detection failed: {str(e)}")
            return []

# Singleton Pattern for Global Use
_detector = None

def detect_space_names(image):
    """Main interface for compatibility with existing project code."""
    global _detector
    if _detector is None:
        _detector = SpatialOCRDetector()
    return _detector.detect_space_names(image)