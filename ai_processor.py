import os
import json
import logging
import google.generativeai as genai
from PIL import Image

logger = logging.getLogger(__name__)

def setup_ai():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("No GEMINI_API_KEY found in .env")
        raise ValueError("Missing GEMINI_API_KEY")
    genai.configure(api_key=api_key)

def generate_wallpaper_metadata(image_path):
    """
    Analyzes an image and returns a JSON dict with Title, Category, Description, and Tags.
    Implements a fallback mechanism across free Gemini models.
    """
    
    prompt = """
    You are an expert wallpaper curator. Analyze this image and provide metadata.
    Output ONLY valid JSON format.
    
    CRITICAL INSTRUCTION: For the "category" field, you MUST choose EXACTLY ONE of the following valid categories:
    "Anime", "Aesthetic", "Nature", "Gaming", "Cars", "Dark", "Minimal", "Abstract", "Space", "City", "Neon", "Technology"
    
    If the image does not fit perfectly, pick the closest one from the list above. Do NOT invent a new category.
    
    Expected JSON structure:
    {
      "title": "A short, catchy, premium title (max 5 words)",
      "category": "One of the exactly specified valid categories above",
      "description": "A beautiful 1-2 sentence description of the wallpaper.",
      "tags": ["tag1", "tag2", "tag3", "tag4"]
    }
    """
    
    # Ordered list of models to try
    models_to_try = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
    
    try:
        img = Image.open(image_path)
    except Exception as e:
        logger.error(f"Failed to open image for AI processing: {e}")
        return {"title": "Premium Wallpaper", "category": "General", "description": "", "tags": []}

    for model_name in models_to_try:
        try:
            logger.info(f"🧠 Attempting AI analysis with model: {model_name}...")
            model = genai.GenerativeModel(model_name, generation_config={"response_mime_type": "application/json"})
            response = model.generate_content([prompt, img])
            
            # Clean up response (sometimes it includes ```json ... ```)
            text = response.text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
                
            text = text.strip()
            metadata = json.loads(text)
            
            # If we get here, the model succeeded
            logger.info(f"✅ Successfully generated metadata using {model_name}")
            return metadata
            
        except Exception as e:
            logger.warning(f"⚠️ Model {model_name} failed: {e}")
            continue # Try the next model
            
    # If all models fail
    logger.error("❌ All Gemini models failed to process the image. Using default metadata.")
    return {
        "title": "Premium Wallpaper",
        "category": "Aesthetic",
        "description": "A beautiful premium wallpaper for your screen.",
        "tags": ["wallpaper", "aesthetic", "4k"]
    }
