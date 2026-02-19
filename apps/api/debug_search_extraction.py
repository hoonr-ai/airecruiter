
import asyncio
import os
import sys

# Add current dir to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
import os

# Adjust path to find .env in apps/api
# We are running from root, so apps/api/.env is correct relative path
load_dotenv("apps/api/.env")

from services.extractor import llm_extractor
from services.unipile import unipile_service

SAMPLE_JD = """
Senior Java Developer
Location: New York, NY
Type: Full-time

We are looking for a Senior Java Developer to join our team. 
You will be working on high-performance systems.

Requirements:
- 5+ years of experience with Java 11+
- Strong knowledge of Spring Boot and Microservices
- Experience with React or Angular frontend
- Knowledge of AWS (EC2, S3, RDS)
- Database experience with PostgreSQL or MySQL
- Excellent communication skills
- Bachelor's degree in Computer Science

Responsibilities:
- Design and implement scalable APIs
- Mentor junior developers
- Code reviews and architectural design
"""

async def test_extraction():
    print("\n--- Testing LLM Extraction ---")
    print(f"Input Text Length: {len(SAMPLE_JD)}")
    
    try:
        data = await llm_extractor.extract_from_jd(SAMPLE_JD)
        print(f"✅ Extraction Successful")
        print(f"Title: {data.title}")
        print(f"Hard Skills ({len(data.hard_skills)}):")
        for s in data.hard_skills:
            print(f"  - {s.name} ({s.seniority}, {s.priority})")
        print(f"Soft Skills ({len(data.soft_skills)}): {data.soft_skills}")
        
    except Exception as e:
        print(f"❌ Extraction Failed: {e}")

async def test_unipile_search():
    print("\n--- Testing Unipile Search (Multiple Skills) ---")
    
    # Test 1: Single Skill
    print("\n1. Searching for ['Java']...")
    results1 = await unipile_service.search_candidates(["Java"], "New York", limit=5)
    print(f"Found {len(results1)} candidates.")

    # Test 2: Multiple Skills (Java AND Python)
    print("\n2. Searching for ['Java', 'Python']...")
    results2 = await unipile_service.search_candidates(["Java", "Python"], "New York", limit=5)
    print(f"Found {len(results2)} candidates.")
    
    # Test 3: Complex Skill Objects
    print("\n3. Searching for Complex Skills...")
    skills = [
        {"name": "Java", "priority": "Must Have"},
        {"name": "React", "priority": "Flexible"}
    ]
    results3 = await unipile_service.search_candidates(skills, "New York", limit=5)
    print(f"Found {len(results3)} candidates.")

    # Test 4: Open To Work
    print("\n4. Searching for ['Java'] with Open To Work...")
    results4 = await unipile_service.search_candidates(["Java"], "New York", open_to_work=True, limit=5)
    print(f"Found {len(results4)} candidates.")

if __name__ == "__main__":
    asyncio.run(test_extraction())
    asyncio.run(test_unipile_search())
