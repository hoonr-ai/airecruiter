import random
from typing import List
from models import JobDescription, CandidateProfile, MatchResult

def mock_match_candidates(jd: JobDescription) -> List[MatchResult]:
    """
    Returns 5 dummy candidates with explainability tags.
    """
    
    # Dummy candidates dataset
    dummy_candidates = [
        CandidateProfile(
            id="c1", name="Sarah Jenkins", email="sarah@example.com",
            skills=["Python", "FastAPI", "React", "Docker", "AWS"],
            experience_years=5, resume_text="Senior Full Stack Engineer..."
        ),
        CandidateProfile(
            id="c2", name="Mike Chen", email="mike@example.com",
            skills=["Java", "Spring Boot", "React", "SQL"],
            experience_years=3, resume_text="Backend focused developer..."
        ),
        CandidateProfile(
            id="c3", name="Jessica Wu", email="jessica@example.com",
            skills=["Python", "Data Science", "Pytorch", "Pandas"],
            experience_years=4, resume_text="AI/ML Engineer..."
        ),
        CandidateProfile(
            id="c4", name="David Kim", email="david@example.com",
            skills=["JavaScript", "Node.js", "Express", "MongoDB"],
            experience_years=2, resume_text="MERN Stack Developer..."
        ),
        CandidateProfile(
            id="c5", name="Emily Davis", email="emily@example.com",
            skills=["Project Management", "Agile", "Scrum"],
            experience_years=7, resume_text="Project Manager..."
        )
    ]

    results = []
    
    # Mock logic: If JD mentions "Python", Python devs get higher score.
    required_set = set(s.lower() for s in jd.required_skills)
    
    for candidate in dummy_candidates:
        candidate_skills = set(s.lower() for s in candidate.skills)
        # Simple intersection
        common = required_set.intersection(candidate_skills)
        missing = required_set - candidate_skills
        
        # Base score random + bonus for skills
        score = random.randint(50, 70)
        if len(required_set) > 0:
            score += int((len(common) / len(required_set)) * 30)
        
        # Explanation
        explanation = []
        if score > 80:
            explanation.append("Strong skill match")
        elif score < 60:
            explanation.append("Low skill overlap")
            
        if missing:
            explanation.append(f"Missing: {', '.join(list(missing)[:2])}")
        else:
            explanation.append("All required skills present")

        results.append(MatchResult(
            candidate=candidate,
            match_percentage=min(score, 100),
            missing_skills=list(missing),
            explainability=explanation
        ))
        
    # Sort by score desc
    results.sort(key=lambda x: x.match_percentage, reverse=True)
    
    return results
