"""The JobDiva pool labels and emitter stamps that the profile-identity trust
rule depends on (services/jobdiva.py ``jobdiva_profile_id``).

A JobDiva pool row is trusted as carrying a real JobDiva profile id when its
``source`` starts with "JobDiva" OR its emitter stamp ``jobdiva_candidate_id``
equals its own id. Renaming a label without keeping one of the two signals turns
JobDiva people into "unknown" people that Launch PAIR would re-create in JobDiva.
"""
import re
from pathlib import Path

from services.jobdiva import jobdiva_profile_id, jobdiva_profile_stamp

SRC = (Path(__file__).parents[1] / "services" / "jobdiva.py").read_text(encoding="utf-8")


def test_every_jobdiva_pool_label_is_trusted():
    labels = set(re.findall(r'"source":\s*"(JobDiva[^"]*)"', SRC))
    assert labels == {"JobDiva", "JobDiva-Applicants", "JobDiva-JobAgent", "JobDiva-TalentSearch"}, labels
    for label in labels:
        assert jobdiva_profile_id(label, "462058065251") == "462058065251", label
    # The frontend's fallback label when a row arrives without one.
    assert jobdiva_profile_id("JobDiva-Applicants", "1") == "1"


def test_every_pool_emitter_stamps_its_own_profile_id():
    """Applicants, JobAgent, TalentSearch (x2 emit sites) and the enhanced
    formatter all splat jobdiva_profile_stamp(candidate_id) into the row."""
    assert SRC.count("**jobdiva_profile_stamp(candidate_id)") >= 5


def test_stamp_only_for_numeric_ids():
    assert jobdiva_profile_stamp("462058065251") == {"jobdiva_candidate_id": "462058065251"}
    assert jobdiva_profile_stamp(462058065251) == {"jobdiva_candidate_id": "462058065251"}
    assert jobdiva_profile_stamp("Unknown") == {}
    assert jobdiva_profile_stamp("") == {}
    assert jobdiva_profile_stamp(None) == {}


def test_trust_is_label_independent_when_the_stamp_matches():
    # Renamed label + matching stamp -> still a JobDiva profile id.
    assert jobdiva_profile_id("PAIR-Agent", "462058065251", "462058065251") == "462058065251"
    assert jobdiva_profile_id(None, "462058065251", " 462058065251 ") == "462058065251"
    # A stamp that disagrees is NOT accepted here (foreign / duplicate id).
    assert jobdiva_profile_id("LinkedIn", "123456", "999") is None
    # A LinkedIn row's numeric-looking id with no stamp stays untrusted.
    assert jobdiva_profile_id("LinkedIn", "123456") is None
    # Non-numeric ids are never profile ids, whatever the signals say.
    assert jobdiva_profile_id("JobDiva", "exa_x", "exa_x") is None
