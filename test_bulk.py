import json
import uuid
import requests
import argparse
import csv
import os
from pathlib import Path

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False


def load_candidates_from_csv(csv_path: str) -> list:
    """
    Load candidates from CSV file.
    Expected columns: name, email, phone, location, title, skills, summary
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    
    candidates = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            candidates.append({
                "name": row.get("name", "").strip(),
                "email": row.get("email", "").strip(),
                "phone": row.get("phone", "").strip(),
                "location": row.get("location", "").strip(),
                "title": row.get("title", "").strip(),
                "skills": row.get("skills", "").strip(),
                "summary": row.get("summary", "").strip(),
            })
    return candidates


def load_candidates_from_excel(excel_path: str, sheet_name=0) -> list:
    """
    Load candidates from Excel file.
    Expected columns: name, email, phone, location, title, skills, summary
    Requires pandas to be installed.
    """
    if not PANDAS_AVAILABLE:
        raise ImportError("pandas is required to read Excel files. Install with: pip install pandas openpyxl")
    
    if not os.path.exists(excel_path):
        raise FileNotFoundError(f"Excel file not found: {excel_path}")
    
    df = pd.read_excel(excel_path, sheet_name=sheet_name)
    
    candidates = []
    for _, row in df.iterrows():
        candidates.append({
            "name": str(row.get("name", "")).strip(),
            "email": str(row.get("email", "")).strip(),
            "phone": str(row.get("phone", "")).strip(),
            "location": str(row.get("location", "")).strip(),
            "title": str(row.get("title", "")).strip(),
            "skills": str(row.get("skills", "")).strip(),
            "summary": str(row.get("summary", "")).strip(),
        })
    return candidates


# ── Deprecated: Dummy candidates (replaced by CSV/Excel loading) ─────────────
# CANDIDATES list has been removed and replaced with load_candidates_from_csv() and load_candidates_from_excel()
# Use the --candidates-file argument to provide your own candidate data in CSV or Excel format



def build_resume(name, email, phone, location, title, skills, summary):
    ("James Miller", "james.miller@email.com", "+12025550008", "Phoenix, AZ", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("James Wilson", "james.wilson@email.com", "+12025550009", "Houston, TX", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("James Moore", "james.moore@email.com", "+12025550010", "San Francisco, CA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("James Taylor", "james.taylor@email.com", "+12025550011", "Atlanta, GA", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("James Thomas", "james.thomas@email.com", "+12025550012", "Dallas, TX", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("James Jackson", "james.jackson@email.com", "+12025550013", "Detroit, MI", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("James White", "james.white@email.com", "+12025550014", "Chicago, IL", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("James Harris", "james.harris@email.com", "+12025550015", "Austin, TX", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("James Martin", "james.martin@email.com", "+12025550016", "Seattle, WA", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("James Robinson", "james.robinson@email.com", "+12025550017", "New York, NY", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("James Clark", "james.clark@email.com", "+12025550018", "Denver, CO", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("James Rodriguez", "james.rodriguez@email.com", "+12025550019", "Boston, MA", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("James Lewis", "james.lewis@email.com", "+12025550020", "Los Angeles, CA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("James Lee", "james.lee@email.com", "+12025550021", "Miami, FL", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("James Walker", "james.walker@email.com", "+12025550022", "Phoenix, AZ", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("James Hall", "james.hall@email.com", "+12025550023", "Houston, TX", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("James Allen", "james.allen@email.com", "+12025550024", "San Francisco, CA", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("James Young", "james.young@email.com", "+12025550025", "Atlanta, GA", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("James Hernandez", "james.hernandez@email.com", "+12025550026", "Dallas, TX", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("James King", "james.king@email.com", "+12025550027", "Detroit, MI", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("James Wright", "james.wright@email.com", "+12025550028", "Chicago, IL", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("James Scott", "james.scott@email.com", "+12025550029", "Austin, TX", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Maria Anderson", "maria.anderson@email.com", "+12025550030", "Seattle, WA", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Maria Garcia", "maria.garcia@email.com", "+12025550031", "New York, NY", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Maria Johnson", "maria.johnson@email.com", "+12025550032", "Denver, CO", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Maria Martinez", "maria.martinez@email.com", "+12025550033", "Boston, MA", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Maria Williams", "maria.williams@email.com", "+12025550034", "Los Angeles, CA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Maria Brown", "maria.brown@email.com", "+12025550035", "Miami, FL", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Maria Jones", "maria.jones@email.com", "+12025550036", "Phoenix, AZ", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Maria Davis", "maria.davis@email.com", "+12025550037", "Houston, TX", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Maria Miller", "maria.miller@email.com", "+12025550038", "San Francisco, CA", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Maria Wilson", "maria.wilson@email.com", "+12025550039", "Atlanta, GA", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Maria Moore", "maria.moore@email.com", "+12025550040", "Dallas, TX", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Maria Taylor", "maria.taylor@email.com", "+12025550041", "Detroit, MI", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Maria Thomas", "maria.thomas@email.com", "+12025550042", "Chicago, IL", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Maria Jackson", "maria.jackson@email.com", "+12025550043", "Austin, TX", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Maria White", "maria.white@email.com", "+12025550044", "Seattle, WA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Maria Harris", "maria.harris@email.com", "+12025550045", "New York, NY", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Maria Martin", "maria.martin@email.com", "+12025550046", "Denver, CO", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Maria Robinson", "maria.robinson@email.com", "+12025550047", "Boston, MA", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Maria Clark", "maria.clark@email.com", "+12025550048", "Los Angeles, CA", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Maria Rodriguez", "maria.rodriguez@email.com", "+12025550049", "Miami, FL", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Maria Lewis", "maria.lewis@email.com", "+12025550050", "Phoenix, AZ", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Maria Lee", "maria.lee@email.com", "+12025550051", "Houston, TX", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Maria Walker", "maria.walker@email.com", "+12025550052", "San Francisco, CA", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Maria Hall", "maria.hall@email.com", "+12025550053", "Atlanta, GA", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Maria Allen", "maria.allen@email.com", "+12025550054", "Dallas, TX", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Maria Young", "maria.young@email.com", "+12025550055", "Detroit, MI", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Maria Hernandez", "maria.hernandez@email.com", "+12025550056", "Chicago, IL", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Maria King", "maria.king@email.com", "+12025550057", "Austin, TX", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Maria Wright", "maria.wright@email.com", "+12025550058", "Seattle, WA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Maria Scott", "maria.scott@email.com", "+12025550059", "New York, NY", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Robert Anderson", "robert.anderson@email.com", "+12025550060", "Denver, CO", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Robert Garcia", "robert.garcia@email.com", "+12025550061", "Boston, MA", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Robert Johnson", "robert.johnson@email.com", "+12025550062", "Los Angeles, CA", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Robert Martinez", "robert.martinez@email.com", "+12025550063", "Miami, FL", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Robert Williams", "robert.williams@email.com", "+12025550064", "Phoenix, AZ", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Robert Brown", "robert.brown@email.com", "+12025550065", "Houston, TX", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Robert Jones", "robert.jones@email.com", "+12025550066", "San Francisco, CA", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Robert Davis", "robert.davis@email.com", "+12025550067", "Atlanta, GA", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Robert Miller", "robert.miller@email.com", "+12025550068", "Dallas, TX", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Robert Wilson", "robert.wilson@email.com", "+12025550069", "Detroit, MI", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Robert Moore", "robert.moore@email.com", "+12025550070", "Chicago, IL", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Robert Taylor", "robert.taylor@email.com", "+12025550071", "Austin, TX", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Robert Thomas", "robert.thomas@email.com", "+12025550072", "Seattle, WA", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Robert Jackson", "robert.jackson@email.com", "+12025550073", "New York, NY", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Robert White", "robert.white@email.com", "+12025550074", "Denver, CO", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Robert Harris", "robert.harris@email.com", "+12025550075", "Boston, MA", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Robert Martin", "robert.martin@email.com", "+12025550076", "Los Angeles, CA", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Robert Robinson", "robert.robinson@email.com", "+12025550077", "Miami, FL", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Robert Clark", "robert.clark@email.com", "+12025550078", "Phoenix, AZ", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Robert Rodriguez", "robert.rodriguez@email.com", "+12025550079", "Houston, TX", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Robert Lewis", "robert.lewis@email.com", "+12025550080", "San Francisco, CA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Robert Lee", "robert.lee@email.com", "+12025550081", "Atlanta, GA", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Robert Walker", "robert.walker@email.com", "+12025550082", "Dallas, TX", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Robert Hall", "robert.hall@email.com", "+12025550083", "Detroit, MI", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Robert Allen", "robert.allen@email.com", "+12025550084", "Chicago, IL", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Robert Young", "robert.young@email.com", "+12025550085", "Austin, TX", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Robert Hernandez", "robert.hernandez@email.com", "+12025550086", "Seattle, WA", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Robert King", "robert.king@email.com", "+12025550087", "New York, NY", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Robert Wright", "robert.wright@email.com", "+12025550088", "Denver, CO", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Robert Scott", "robert.scott@email.com", "+12025550089", "Boston, MA", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Linda Anderson", "linda.anderson@email.com", "+12025550090", "Los Angeles, CA", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Linda Garcia", "linda.garcia@email.com", "+12025550091", "Miami, FL", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Linda Johnson", "linda.johnson@email.com", "+12025550092", "Phoenix, AZ", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Linda Martinez", "linda.martinez@email.com", "+12025550093", "Houston, TX", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Linda Williams", "linda.williams@email.com", "+12025550094", "San Francisco, CA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Linda Brown", "linda.brown@email.com", "+12025550095", "Atlanta, GA", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Linda Jones", "linda.jones@email.com", "+12025550096", "Dallas, TX", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Linda Davis", "linda.davis@email.com", "+12025550097", "Detroit, MI", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Linda Miller", "linda.miller@email.com", "+12025550098", "Chicago, IL", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Linda Wilson", "linda.wilson@email.com", "+12025550099", "Austin, TX", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Linda Moore", "linda.moore@email.com", "+12025550100", "Seattle, WA", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Linda Taylor", "linda.taylor@email.com", "+12025550101", "New York, NY", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Linda Thomas", "linda.thomas@email.com", "+12025550102", "Denver, CO", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Linda Jackson", "linda.jackson@email.com", "+12025550103", "Boston, MA", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Linda White", "linda.white@email.com", "+12025550104", "Los Angeles, CA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Linda Harris", "linda.harris@email.com", "+12025550105", "Miami, FL", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Linda Martin", "linda.martin@email.com", "+12025550106", "Phoenix, AZ", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Linda Robinson", "linda.robinson@email.com", "+12025550107", "Houston, TX", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Linda Clark", "linda.clark@email.com", "+12025550108", "San Francisco, CA", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Linda Rodriguez", "linda.rodriguez@email.com", "+12025550109", "Atlanta, GA", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Linda Lewis", "linda.lewis@email.com", "+12025550110", "Dallas, TX", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Linda Lee", "linda.lee@email.com", "+12025550111", "Detroit, MI", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Linda Walker", "linda.walker@email.com", "+12025550112", "Chicago, IL", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Linda Hall", "linda.hall@email.com", "+12025550113", "Austin, TX", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Linda Allen", "linda.allen@email.com", "+12025550114", "Seattle, WA", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Linda Young", "linda.young@email.com", "+12025550115", "New York, NY", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Linda Hernandez", "linda.hernandez@email.com", "+12025550116", "Denver, CO", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Linda King", "linda.king@email.com", "+12025550117", "Boston, MA", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Linda Wright", "linda.wright@email.com", "+12025550118", "Los Angeles, CA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Linda Scott", "linda.scott@email.com", "+12025550119", "Miami, FL", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Michael Anderson", "michael.anderson@email.com", "+12025550120", "Phoenix, AZ", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Michael Garcia", "michael.garcia@email.com", "+12025550121", "Houston, TX", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Michael Johnson", "michael.johnson@email.com", "+12025550122", "San Francisco, CA", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Michael Martinez", "michael.martinez@email.com", "+12025550123", "Atlanta, GA", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Michael Williams", "michael.williams@email.com", "+12025550124", "Dallas, TX", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Michael Brown", "michael.brown@email.com", "+12025550125", "Detroit, MI", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Michael Jones", "michael.jones@email.com", "+12025550126", "Chicago, IL", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Michael Davis", "michael.davis@email.com", "+12025550127", "Austin, TX", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Michael Miller", "michael.miller@email.com", "+12025550128", "Seattle, WA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Michael Wilson", "michael.wilson@email.com", "+12025550129", "New York, NY", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Michael Moore", "michael.moore@email.com", "+12025550130", "Denver, CO", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Michael Taylor", "michael.taylor@email.com", "+12025550131", "Boston, MA", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Michael Thomas", "michael.thomas@email.com", "+12025550132", "Los Angeles, CA", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Michael Jackson", "michael.jackson@email.com", "+12025550133", "Miami, FL", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Michael White", "michael.white@email.com", "+12025550134", "Phoenix, AZ", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Michael Harris", "michael.harris@email.com", "+12025550135", "Houston, TX", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Michael Martin", "michael.martin@email.com", "+12025550136", "San Francisco, CA", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Michael Robinson", "michael.robinson@email.com", "+12025550137", "Atlanta, GA", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Michael Clark", "michael.clark@email.com", "+12025550138", "Dallas, TX", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Michael Rodriguez", "michael.rodriguez@email.com", "+12025550139", "Detroit, MI", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Michael Lewis", "michael.lewis@email.com", "+12025550140", "Chicago, IL", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Michael Lee", "michael.lee@email.com", "+12025550141", "Austin, TX", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Michael Walker", "michael.walker@email.com", "+12025550142", "Seattle, WA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Michael Hall", "michael.hall@email.com", "+12025550143", "New York, NY", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Michael Allen", "michael.allen@email.com", "+12025550144", "Denver, CO", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Michael Young", "michael.young@email.com", "+12025550145", "Boston, MA", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Michael Hernandez", "michael.hernandez@email.com", "+12025550146", "Los Angeles, CA", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Michael King", "michael.king@email.com", "+12025550147", "Miami, FL", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Michael Wright", "michael.wright@email.com", "+12025550148", "Phoenix, AZ", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Michael Scott", "michael.scott@email.com", "+12025550149", "Houston, TX", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Patricia Anderson", "patricia.anderson@email.com", "+12025550150", "San Francisco, CA", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Patricia Garcia", "patricia.garcia@email.com", "+12025550151", "Atlanta, GA", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Patricia Johnson", "patricia.johnson@email.com", "+12025550152", "Dallas, TX", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Patricia Martinez", "patricia.martinez@email.com", "+12025550153", "Detroit, MI", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Patricia Williams", "patricia.williams@email.com", "+12025550154", "Chicago, IL", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Patricia Brown", "patricia.brown@email.com", "+12025550155", "Austin, TX", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Patricia Jones", "patricia.jones@email.com", "+12025550156", "Seattle, WA", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Patricia Davis", "patricia.davis@email.com", "+12025550157", "New York, NY", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Patricia Miller", "patricia.miller@email.com", "+12025550158", "Denver, CO", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Patricia Wilson", "patricia.wilson@email.com", "+12025550159", "Boston, MA", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Patricia Moore", "patricia.moore@email.com", "+12025550160", "Los Angeles, CA", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Patricia Taylor", "patricia.taylor@email.com", "+12025550161", "Miami, FL", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Patricia Thomas", "patricia.thomas@email.com", "+12025550162", "Phoenix, AZ", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Patricia Jackson", "patricia.jackson@email.com", "+12025550163", "Houston, TX", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Patricia White", "patricia.white@email.com", "+12025550164", "San Francisco, CA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Patricia Harris", "patricia.harris@email.com", "+12025550165", "Atlanta, GA", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Patricia Martin", "patricia.martin@email.com", "+12025550166", "Dallas, TX", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Patricia Robinson", "patricia.robinson@email.com", "+12025550167", "Detroit, MI", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Patricia Clark", "patricia.clark@email.com", "+12025550168", "Chicago, IL", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Patricia Rodriguez", "patricia.rodriguez@email.com", "+12025550169", "Austin, TX", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Patricia Lewis", "patricia.lewis@email.com", "+12025550170", "Seattle, WA", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Patricia Lee", "patricia.lee@email.com", "+12025550171", "New York, NY", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Patricia Walker", "patricia.walker@email.com", "+12025550172", "Denver, CO", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Patricia Hall", "patricia.hall@email.com", "+12025550173", "Boston, MA", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Patricia Allen", "patricia.allen@email.com", "+12025550174", "Los Angeles, CA", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Patricia Young", "patricia.young@email.com", "+12025550175", "Miami, FL", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Patricia Hernandez", "patricia.hernandez@email.com", "+12025550176", "Phoenix, AZ", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Patricia King", "patricia.king@email.com", "+12025550177", "Houston, TX", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Patricia Wright", "patricia.wright@email.com", "+12025550178", "San Francisco, CA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Patricia Scott", "patricia.scott@email.com", "+12025550179", "Atlanta, GA", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("David Anderson", "david.anderson@email.com", "+12025550180", "Dallas, TX", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("David Garcia", "david.garcia@email.com", "+12025550181", "Detroit, MI", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("David Johnson", "david.johnson@email.com", "+12025550182", "Chicago, IL", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("David Martinez", "david.martinez@email.com", "+12025550183", "Austin, TX", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("David Williams", "david.williams@email.com", "+12025550184", "Seattle, WA", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("David Brown", "david.brown@email.com", "+12025550185", "New York, NY", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("David Jones", "david.jones@email.com", "+12025550186", "Denver, CO", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("David Davis", "david.davis@email.com", "+12025550187", "Boston, MA", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("David Miller", "david.miller@email.com", "+12025550188", "Los Angeles, CA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("David Wilson", "david.wilson@email.com", "+12025550189", "Miami, FL", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("David Moore", "david.moore@email.com", "+12025550190", "Phoenix, AZ", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("David Taylor", "david.taylor@email.com", "+12025550191", "Houston, TX", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("David Thomas", "david.thomas@email.com", "+12025550192", "San Francisco, CA", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("David Jackson", "david.jackson@email.com", "+12025550193", "Atlanta, GA", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("David White", "david.white@email.com", "+12025550194", "Dallas, TX", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("David Harris", "david.harris@email.com", "+12025550195", "Detroit, MI", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("David Martin", "david.martin@email.com", "+12025550196", "Chicago, IL", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("David Robinson", "david.robinson@email.com", "+12025550197", "Austin, TX", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("David Clark", "david.clark@email.com", "+12025550198", "Seattle, WA", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("David Rodriguez", "david.rodriguez@email.com", "+12025550199", "New York, NY", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("David Lewis", "david.lewis@email.com", "+12025550200", "Denver, CO", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("David Lee", "david.lee@email.com", "+12025550201", "Boston, MA", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("David Walker", "david.walker@email.com", "+12025550202", "Los Angeles, CA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("David Hall", "david.hall@email.com", "+12025550203", "Miami, FL", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("David Allen", "david.allen@email.com", "+12025550204", "Phoenix, AZ", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("David Young", "david.young@email.com", "+12025550205", "Houston, TX", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("David Hernandez", "david.hernandez@email.com", "+12025550206", "San Francisco, CA", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("David King", "david.king@email.com", "+12025550207", "Atlanta, GA", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("David Wright", "david.wright@email.com", "+12025550208", "Dallas, TX", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("David Scott", "david.scott@email.com", "+12025550209", "Detroit, MI", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Barbara Anderson", "barbara.anderson@email.com", "+12025550210", "Chicago, IL", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Barbara Garcia", "barbara.garcia@email.com", "+12025550211", "Austin, TX", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Barbara Johnson", "barbara.johnson@email.com", "+12025550212", "Seattle, WA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Barbara Martinez", "barbara.martinez@email.com", "+12025550213", "New York, NY", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Barbara Williams", "barbara.williams@email.com", "+12025550214", "Denver, CO", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Barbara Brown", "barbara.brown@email.com", "+12025550215", "Boston, MA", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Barbara Jones", "barbara.jones@email.com", "+12025550216", "Los Angeles, CA", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Barbara Davis", "barbara.davis@email.com", "+12025550217", "Miami, FL", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Barbara Miller", "barbara.miller@email.com", "+12025550218", "Phoenix, AZ", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Barbara Wilson", "barbara.wilson@email.com", "+12025550219", "Houston, TX", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Barbara Moore", "barbara.moore@email.com", "+12025550220", "San Francisco, CA", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Barbara Taylor", "barbara.taylor@email.com", "+12025550221", "Atlanta, GA", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Barbara Thomas", "barbara.thomas@email.com", "+12025550222", "Dallas, TX", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Barbara Jackson", "barbara.jackson@email.com", "+12025550223", "Detroit, MI", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Barbara White", "barbara.white@email.com", "+12025550224", "Chicago, IL", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Barbara Harris", "barbara.harris@email.com", "+12025550225", "Austin, TX", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Barbara Martin", "barbara.martin@email.com", "+12025550226", "Seattle, WA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Barbara Robinson", "barbara.robinson@email.com", "+12025550227", "New York, NY", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Barbara Clark", "barbara.clark@email.com", "+12025550228", "Denver, CO", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Barbara Rodriguez", "barbara.rodriguez@email.com", "+12025550229", "Boston, MA", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Barbara Lewis", "barbara.lewis@email.com", "+12025550230", "Los Angeles, CA", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Barbara Lee", "barbara.lee@email.com", "+12025550231", "Miami, FL", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Barbara Walker", "barbara.walker@email.com", "+12025550232", "Phoenix, AZ", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Barbara Hall", "barbara.hall@email.com", "+12025550233", "Houston, TX", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Barbara Allen", "barbara.allen@email.com", "+12025550234", "San Francisco, CA", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Barbara Young", "barbara.young@email.com", "+12025550235", "Atlanta, GA", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Barbara Hernandez", "barbara.hernandez@email.com", "+12025550236", "Dallas, TX", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Barbara King", "barbara.king@email.com", "+12025550237", "Detroit, MI", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Barbara Wright", "barbara.wright@email.com", "+12025550238", "Chicago, IL", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Barbara Scott", "barbara.scott@email.com", "+12025550239", "Austin, TX", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Richard Anderson", "richard.anderson@email.com", "+12025550240", "Seattle, WA", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Richard Garcia", "richard.garcia@email.com", "+12025550241", "New York, NY", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Richard Johnson", "richard.johnson@email.com", "+12025550242", "Denver, CO", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Richard Martinez", "richard.martinez@email.com", "+12025550243", "Boston, MA", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Richard Williams", "richard.williams@email.com", "+12025550244", "Los Angeles, CA", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Richard Brown", "richard.brown@email.com", "+12025550245", "Miami, FL", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Richard Jones", "richard.jones@email.com", "+12025550246", "Phoenix, AZ", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Richard Davis", "richard.davis@email.com", "+12025550247", "Houston, TX", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Richard Miller", "richard.miller@email.com", "+12025550248", "San Francisco, CA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Richard Wilson", "richard.wilson@email.com", "+12025550249", "Atlanta, GA", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Richard Moore", "richard.moore@email.com", "+12025550250", "Dallas, TX", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Richard Taylor", "richard.taylor@email.com", "+12025550251", "Detroit, MI", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Richard Thomas", "richard.thomas@email.com", "+12025550252", "Chicago, IL", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Richard Jackson", "richard.jackson@email.com", "+12025550253", "Austin, TX", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Richard White", "richard.white@email.com", "+12025550254", "Seattle, WA", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Richard Harris", "richard.harris@email.com", "+12025550255", "New York, NY", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Richard Martin", "richard.martin@email.com", "+12025550256", "Denver, CO", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Richard Robinson", "richard.robinson@email.com", "+12025550257", "Boston, MA", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Richard Clark", "richard.clark@email.com", "+12025550258", "Los Angeles, CA", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Richard Rodriguez", "richard.rodriguez@email.com", "+12025550259", "Miami, FL", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Richard Lewis", "richard.lewis@email.com", "+12025550260", "Phoenix, AZ", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Richard Lee", "richard.lee@email.com", "+12025550261", "Houston, TX", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Richard Walker", "richard.walker@email.com", "+12025550262", "San Francisco, CA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Richard Hall", "richard.hall@email.com", "+12025550263", "Atlanta, GA", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Richard Allen", "richard.allen@email.com", "+12025550264", "Dallas, TX", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Richard Young", "richard.young@email.com", "+12025550265", "Detroit, MI", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Richard Hernandez", "richard.hernandez@email.com", "+12025550266", "Chicago, IL", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Richard King", "richard.king@email.com", "+12025550267", "Austin, TX", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Richard Wright", "richard.wright@email.com", "+12025550268", "Seattle, WA", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Richard Scott", "richard.scott@email.com", "+12025550269", "New York, NY", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Jennifer Anderson", "jennifer.anderson@email.com", "+12025550270", "Denver, CO", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Jennifer Garcia", "jennifer.garcia@email.com", "+12025550271", "Boston, MA", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Jennifer Johnson", "jennifer.johnson@email.com", "+12025550272", "Los Angeles, CA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Jennifer Martinez", "jennifer.martinez@email.com", "+12025550273", "Miami, FL", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Jennifer Williams", "jennifer.williams@email.com", "+12025550274", "Phoenix, AZ", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Jennifer Brown", "jennifer.brown@email.com", "+12025550275", "Houston, TX", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Jennifer Jones", "jennifer.jones@email.com", "+12025550276", "San Francisco, CA", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Jennifer Davis", "jennifer.davis@email.com", "+12025550277", "Atlanta, GA", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Jennifer Miller", "jennifer.miller@email.com", "+12025550278", "Dallas, TX", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Jennifer Wilson", "jennifer.wilson@email.com", "+12025550279", "Detroit, MI", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Jennifer Moore", "jennifer.moore@email.com", "+12025550280", "Chicago, IL", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Jennifer Taylor", "jennifer.taylor@email.com", "+12025550281", "Austin, TX", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Jennifer Thomas", "jennifer.thomas@email.com", "+12025550282", "Seattle, WA", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Jennifer Jackson", "jennifer.jackson@email.com", "+12025550283", "New York, NY", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Jennifer White", "jennifer.white@email.com", "+12025550284", "Denver, CO", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Jennifer Harris", "jennifer.harris@email.com", "+12025550285", "Boston, MA", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
    ("Jennifer Martin", "jennifer.martin@email.com", "+12025550286", "Los Angeles, CA", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Los Angeles, CA. Skilled in multiple modern frameworks."),
    ("Jennifer Robinson", "jennifer.robinson@email.com", "+12025550287", "Miami, FL", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Miami, FL. Skilled in multiple modern frameworks."),
    ("Jennifer Clark", "jennifer.clark@email.com", "+12025550288", "Phoenix, AZ", "Software Engineer", "Python, SQL, React, Node.js, Software Engineer Skills, Agile, Communication", "Experienced Software Engineer with a demonstrated history of working in the technology sector in Phoenix, AZ. Skilled in multiple modern frameworks."),
    ("Jennifer Rodriguez", "jennifer.rodriguez@email.com", "+12025550289", "Houston, TX", "Data Scientist", "Python, SQL, React, Node.js, Data Scientist Skills, Agile, Communication", "Experienced Data Scientist with a demonstrated history of working in the technology sector in Houston, TX. Skilled in multiple modern frameworks."),
    ("Jennifer Lewis", "jennifer.lewis@email.com", "+12025550290", "San Francisco, CA", "DevOps Engineer", "Python, SQL, React, Node.js, DevOps Engineer Skills, Agile, Communication", "Experienced DevOps Engineer with a demonstrated history of working in the technology sector in San Francisco, CA. Skilled in multiple modern frameworks."),
    ("Jennifer Lee", "jennifer.lee@email.com", "+12025550291", "Atlanta, GA", "Product Manager", "Python, SQL, React, Node.js, Product Manager Skills, Agile, Communication", "Experienced Product Manager with a demonstrated history of working in the technology sector in Atlanta, GA. Skilled in multiple modern frameworks."),
    ("Jennifer Walker", "jennifer.walker@email.com", "+12025550292", "Dallas, TX", "QA Automation Engineer", "Python, SQL, React, Node.js, QA Automation Engineer Skills, Agile, Communication", "Experienced QA Automation Engineer with a demonstrated history of working in the technology sector in Dallas, TX. Skilled in multiple modern frameworks."),
    ("Jennifer Hall", "jennifer.hall@email.com", "+12025550293", "Detroit, MI", "UX Designer", "Python, SQL, React, Node.js, UX Designer Skills, Agile, Communication", "Experienced UX Designer with a demonstrated history of working in the technology sector in Detroit, MI. Skilled in multiple modern frameworks."),
    ("Jennifer Allen", "jennifer.allen@email.com", "+12025550294", "Chicago, IL", "Backend Developer", "Python, SQL, React, Node.js, Backend Developer Skills, Agile, Communication", "Experienced Backend Developer with a demonstrated history of working in the technology sector in Chicago, IL. Skilled in multiple modern frameworks."),
    ("Jennifer Young", "jennifer.young@email.com", "+12025550295", "Austin, TX", "Project Manager", "Python, SQL, React, Node.js, Project Manager Skills, Agile, Communication", "Experienced Project Manager with a demonstrated history of working in the technology sector in Austin, TX. Skilled in multiple modern frameworks."),
    ("Jennifer Hernandez", "jennifer.hernandez@email.com", "+12025550296", "Seattle, WA", "Full Stack Developer", "Python, SQL, React, Node.js, Full Stack Developer Skills, Agile, Communication", "Experienced Full Stack Developer with a demonstrated history of working in the technology sector in Seattle, WA. Skilled in multiple modern frameworks."),
    ("Jennifer King", "jennifer.king@email.com", "+12025550297", "New York, NY", "Cloud Architect", "Python, SQL, React, Node.js, Cloud Architect Skills, Agile, Communication", "Experienced Cloud Architect with a demonstrated history of working in the technology sector in New York, NY. Skilled in multiple modern frameworks."),
    ("Jennifer Wright", "jennifer.wright@email.com", "+12025550298", "Denver, CO", "Security Analyst", "Python, SQL, React, Node.js, Security Analyst Skills, Agile, Communication", "Experienced Security Analyst with a demonstrated history of working in the technology sector in Denver, CO. Skilled in multiple modern frameworks."),
    ("Jennifer Scott", "jennifer.scott@email.com", "+12025550299", "Boston, MA", "System Admin", "Python, SQL, React, Node.js, System Admin Skills, Agile, Communication", "Experienced System Admin with a demonstrated history of working in the technology sector in Boston, MA. Skilled in multiple modern frameworks."),
]



def build_resume(name, email, phone, location, title, skills, summary):
    first, *rest = name.split()
    last = " ".join(rest) if rest else ""
    return f"""{name}
{location}  |  {phone}  |  {email}

{title.upper()}

{summary}

CORE SKILLS
{skills}

PROFESSIONAL EXPERIENCE
Senior {title}
Various clients | 2018–Present
- Delivered high-impact solutions across multiple engagements.
- Collaborated with cross-functional stakeholders to drive outcomes.
- Mentored junior team members and contributed to knowledge sharing.

{title}
Tech Corp | 2014–2018
- Contributed to product development and operational improvements.
- Improved key performance metrics through data-driven decisions.
- Led proof-of-concept initiatives that became production systems.

EDUCATION
B.S. Computer Science / Related Field
State University | 2010–2014

CERTIFICATIONS
Relevant industry certifications held and maintained.
"""


def test_bulk_interview(api_url="http://localhost:8001", candidates_file=None):
    """
    Test bulk interview with candidates from CSV/Excel file.
    Sends interviews through the local API /api/engage/send-bulk-interview endpoint in batches.
    
    Args:
        api_url: Base URL of the API (e.g., http://localhost:8001)
        candidates_file: Path to CSV or Excel file with candidate data
    """
    print("\n" + "="*60)
    print("  BULK INTERVIEW LOAD TEST")
    print("="*60)

    # Load candidates from file
    if not candidates_file:
        raise ValueError("--candidates-file is required. Provide a CSV or Excel file path.")
    
    file_ext = Path(candidates_file).suffix.lower()
    
    if file_ext == '.csv':
        print(f"\n📁 Loading candidates from CSV: {candidates_file}")
        candidates_data = load_candidates_from_csv(candidates_file)
    elif file_ext in ['.xlsx', '.xls']:
        print(f"\n📁 Loading candidates from Excel: {candidates_file}")
        candidates_data = load_candidates_from_excel(candidates_file)
    else:
        raise ValueError(f"Unsupported file format: {file_ext}. Use .csv or .xlsx")
    
    if not candidates_data:
        raise ValueError(f"No candidates found in {candidates_file}")
    
    print(f"  ✅ Loaded {len(candidates_data)} candidates")

    with open("samplepayload.json", "r") as f:
        base_payload = json.load(f)

    jd = base_payload["jd"]
    resumes = []
    real_candidate_ids = []

    print(f"\n🔧 Building candidate profiles from file...")
    for i, cand_data in enumerate(candidates_data, 1):
        name = cand_data.get("name", f"Candidate {i}")
        email = cand_data.get("email", f"candidate{i}@example.com")
        phone = cand_data.get("phone", f"+1202555{i:04d}")
        location = cand_data.get("location", "Remote")
        title = cand_data.get("title", "Engineer")
        skills = cand_data.get("skills", "Various technical skills")
        summary = cand_data.get("summary", "Skilled professional")
        
        cid = f"candidate_{i:03d}_{uuid.uuid4().hex[:6]}"
        
        first, *rest = name.split()
        last = " ".join(rest) if rest else ""

        resumes.append({
            "source_candidate_id": cid,
            "name": name,
            "candidate_name": name,
            "full_name": name,
            "first_name": first,
            "last_name": last,
            "email": email,
            "phone": phone,
            "raw_resume_text": build_resume(name, email, phone, location, title, skills, summary),
            "experience": f"{title} — {location}. {summary}",
            "summary": summary,
            "skills": skills,
            "education": "B.S. Computer Science",
        })
        real_candidate_ids.append(cid)
        print(f"  ✅ [{i:>3}/{len(candidates_data)}] {name} — {title}")

    from datetime import datetime
    test_run_label = datetime.now().strftime("%-d %b %Y %-I:%M:%S %p IST")

    # Hardcoded distinguishable test job — easy to find in DB / pairbotqa
    test_jd = {
        "job_id": "TEST_JOB_BULK_300",
        "jobdiva_id": "TEST-BULK-300",
        "context": {
            "title": f"[TEST] Bulk Interview Load Test — 300 Candidates ({test_run_label})",
            "customer_name": "PAIR Internal QA",
            "city": "Remote",
            "state": "NA",
            "location_type": "Remote",
            "jobdiva_description": "This is a synthetic bulk interview load test run by the PAIR engineering team to validate 150-candidate batch processing.",
            "ai_description": "Automated QA load test — not a real job posting.",
            "recruiter_notes": "Do not engage. Test run only.",
        },
        "rubric": {
            "titles": [{"value": "QA Load Test Role", "minYears": 0, "recent": False, "matchType": "Exact", "required": "Required", "source": "Test"}],
            "skills": [{"value": "Bulk Interview Testing", "minYears": 1, "recent": True, "matchType": "Exact", "required": "Required"}],
            "education": [{"degree": "Any", "field": "Any", "required": "Optional"}],
        },
        "pre_screen_questions": [
            {"question_text": "This is a test interview. Can you confirm you are a test candidate?", "pass_criteria": "Answers yes", "is_default": True, "category": "test"},
        ],
    }

    BATCH_SIZE = 5
    batches = []
    for i in range(0, len(resumes), BATCH_SIZE):
        batch_resumes = resumes[i:i + BATCH_SIZE]
        batch_cids = real_candidate_ids[i:i + BATCH_SIZE]
        batches.append((batch_resumes, batch_cids))

    print(f"\n📦 Grouped {len(resumes)} candidates into {len(batches)} batches of {BATCH_SIZE}.")

    endpoint = f"{api_url.rstrip('/')}/api/engage/send-bulk-interview"
    if "pairbotqa.hoonr.ai" in api_url or "pairqa.hoonr.ai" in api_url:
        endpoint = f"{api_url.rstrip('/')}/api/bulk-interviews"

    print(f"📤 Hitting: POST {endpoint}")

    total_results = []
    total_skipped = []
    failed_batches = 0

    import concurrent.futures
    import time as _time

    def send_batch(batch_idx, batch_resumes, batch_cids):
        payload_obj = {
            "resumes": batch_resumes,
            "jd": test_jd,
            "company_intro": "PAIR Internal QA — Bulk Load Test",
            "interview_duration": "20-25",
            "source": test_run_label,
        }
        
        request_body = {
            "payload": json.dumps(payload_obj),
            "real_candidate_ids": batch_cids,
            "is_initial_launch": False,
            "dry_run": False,
            "notify_recruiters": False,
            "app_base_url": api_url,
        }

        # If hitting the external PAIR API directly, use payload_obj.
        # If hitting the internal AI Recruiter API, use request_body.
        json_payload = payload_obj if "bulk-interviews" in endpoint else request_body

        try:
            response = requests.post(endpoint, json=json_payload, timeout=180)
            try:
                return batch_idx, response.status_code, response.json()
            except Exception:
                # Server returned non-JSON — log raw text for diagnosis
                raw = response.text[:500] if response.text else "<empty body>"
                return batch_idx, response.status_code, {"raw_error": raw}
        except Exception as e:
            return batch_idx, None, {"raw_error": str(e)}

    # Production Curate flow processes batches SEQUENTIALLY (one at a time),
    # using a for-loop with await — NOT Promise.all / concurrent.
    # Source: apps/web/app/jobs/new/page.tsx line 5996
    for batch_idx, (b_resumes, b_cids) in enumerate(batches):
        b_idx, status_code, data = send_batch(batch_idx, b_resumes, b_cids)
        if status_code == 200:
            success = data.get("success", False)
            if success:
                results = data.get("data", [])
                skipped = data.get("skipped_already_sent", [])
                total_results.extend(results)
                total_skipped.extend(skipped)
                print(f"  ✅ Batch {batch_idx+1}/{len(batches)} SUCCESS: {len(results)} created, {len(skipped)} skipped.")
            else:
                failed_batches += 1
                print(f"  ❌ Batch {batch_idx+1}/{len(batches)} FAILED: {data.get('message', 'Unknown error')}")
        else:
            failed_batches += 1
            raw_err = data.get('raw_error', data) if isinstance(data, dict) else data
            print(f"  ❌ Batch {batch_idx+1}/{len(batches)} HTTP {status_code}: {raw_err}")

    print(f"\n📋 Interview Results (first 10):")
    for r in total_results[:10]:
        print(f"   [{r.get('candidate_name','?')}] interview_id={r.get('interview_id','—')}  email={r.get('candidate_email','?')}")
    if len(total_results) > 10:
        print(f"   ... and {len(total_results)-10} more")

    # Summary table
    print(f"\n{'='*60}")
    print(f"  BULK INTERVIEW TEST SUMMARY")
    print(f"{'='*60}")
    print(f"  Candidates sent    : {len(resumes)}")
    print(f"  Interviews created : {len(total_results)}")
    print(f"  Skipped (already)  : {len(total_skipped)}")
    print(f"  Failed Batches     : {failed_batches}")
    print(f"  Status             : {'✅ PASSED' if failed_batches == 0 and len(total_results) > 0 else '❌ FAILED'}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test bulk interview with candidates from CSV/Excel file")
    parser.add_argument("--url", type=str, default="http://localhost:8001", help="Base URL of the local API")
    parser.add_argument("--candidates-file", type=str, required=True, help="Path to CSV or Excel file with candidate data")
    args = parser.parse_args()
    test_bulk_interview(api_url=args.url, candidates_file=args.candidates_file)
