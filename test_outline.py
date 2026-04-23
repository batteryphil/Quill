import asyncio
import json
import re

async def main():
    import sys
    import os
    sys.path.append("/home/phil/.gemini/antigravity/scratch/quill")
    from backend.bookwriter import _generate_outline, _OUTLINE_SYSTEM
    
    config = {
        "premise": "A detective Elias Thorne solving a murder on Mars.",
        "genre": "Sci-Fi Thriller",
        "pov": "third person limited",
        "num_chapters": 5,
        "scenes_per_chapter": 2,
        "tone": "dark"
    }
    print("Generating outline...")
    out = await _generate_outline(config)
    print("FINAL PARSED dict:")
    print(json.dumps(out, indent=2))
    
if __name__ == "__main__":
    asyncio.run(main())
