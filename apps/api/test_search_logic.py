import asyncio
import os
import sys
from dotenv import load_dotenv

# Load env vars FIRST so service picks them up at import time
load_dotenv("apps/api/.env")

# Add parent to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.api.services.jobdiva import jobdiva_service

async def test_waterfall_logic():
    print("🚀 Testing JobDiva Waterfall Logic (Min 5 Threshold)")
    
    # Test Case: "SQL" - previously returned 1 result with Strict execution
    # Now should return more via Loose fallback
    skills = ["SQL"] 
    location = "" # Empty string for Remote (no state filter)
    
    print(f"👉 Searching for: {skills} Location: {location}")
    
    try:
        results = await jobdiva_service.search_candidates(skills, location, 1, 100)
        
        print("\n✅ Search Completed")
        print(f"📊 Total Candidates Found: {len(results)}")
        
        if len(results) == 0:
            print("❌ Found 0 candidates. Search failed completely.")
        elif len(results) < 5:
            # Check if this behavior is expected (maybe truly only 2 candidates exist?)
            # But the goal was to confirm loose search ran.
            # We can't see logs easily here unless we enable logging.
            print(f"⚠️ Found {len(results)} candidates. Waterfall might not have triggered fallback properly if > 0 matches exist in Loose.")
        else:
            print(f"🎉 SUCCESS: Found {len(results)} candidates! (Should be > 5)")
            
        # Print first 3 names to verify diversity
        for i, c in enumerate(results[:3]):
            print(f"   {i+1}. {c.get('firstName')} {c.get('lastName')} - {c.get('city')}")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_waterfall_logic())
