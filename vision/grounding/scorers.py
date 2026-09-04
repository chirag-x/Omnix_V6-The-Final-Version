import re
from typing import Optional, Any
from vision.observations.targets import TargetCandidate
from vision.grounding.models import TargetSpec

def normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
    # Lowercase, remove extra whitespace, punctuation, and common articles
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\b(the|a|an)\b', '', text)
    return " ".join(text.split())

def score_semantic_match(spec: TargetSpec, candidate: TargetCandidate) -> float:
    """Score how well the candidate matches the semantic name/text."""
    spec_name = normalize_text(spec.semantic_name)
    spec_text = normalize_text(spec.text)
    cand_name = normalize_text(candidate.name)
    cand_text = normalize_text(candidate.text)
    
    score = 0.0
    # Exact match on name
    if spec_name and (spec_name == cand_name):
        score = max(score, 1.0)
    elif spec_name and (spec_name in cand_name or cand_name in spec_name) and len(cand_name) > 0:
        score = max(score, 0.7)
        
    # Exact match on text
    if spec_text and (spec_text == cand_text):
        score = max(score, 1.0)
    elif spec_text and (spec_text in cand_text or cand_text in spec_text) and len(cand_text) > 0:
        score = max(score, 0.7)
        
    # Cross-match name vs text
    if spec_name and cand_text and (spec_name == cand_text):
        score = max(score, 0.9)
    elif spec_text and cand_name and (spec_text == cand_name):
        score = max(score, 0.9)
        
    # Substring matches
    if spec_name and cand_text and (spec_name in cand_text or cand_text in spec_name) and len(cand_text) > 0:
        score = max(score, 0.6)
        
    return score

def score_role_match(spec: TargetSpec, candidate: TargetCandidate) -> float:
    """Score how well the candidate role matches the requested role."""
    if not spec.role:
        return 0.5  # Neutral if not requested
        
    spec_role = normalize_text(spec.role)
    
    # Extract role from candidate
    cand_role = normalize_text(candidate.role or candidate.element_type)
    
    if spec_role == cand_role:
        return 1.0
        
    # Common synonyms mapping (minimal, not application specific)
    role_synonyms = {
        "button": ["btn", "button", "pushbutton"],
        "input": ["edit", "textfield", "textbox", "input", "searchbox"],
        "link": ["hyperlink", "link", "a"],
        "checkbox": ["check", "checkbox", "toggle"],
    }
    
    for _, syns in role_synonyms.items():
        if spec_role in syns and cand_role in syns:
            return 1.0
            
    return 0.0

def score_state_match(spec: TargetSpec, candidate: TargetCandidate) -> float:
    if not spec.state:
        return 1.0  # Perfect if no state specified
        
    score = 1.0
    if 'enabled' in spec.state and spec.state['enabled'] != candidate.enabled:
        score *= 0.0
    if 'visible' in spec.state and spec.state['visible'] != candidate.visible:
        score *= 0.0
        
    return score

def get_candidate_center(candidate: TargetCandidate) -> tuple[float, float]:
    left, top, right, bottom = candidate.bbox
    return ((left + right) / 2.0, (top + bottom) / 2.0)

def score_spatial_relationship(candidate: TargetCandidate, relationship: Any, reference_candidate: TargetCandidate) -> float:
    """Score candidate based on spatial relationship to a reference candidate."""
    rel = relationship.relation_type
    
    cand_center = get_candidate_center(candidate)
    ref_center = get_candidate_center(reference_candidate)
    
    dx = cand_center[0] - ref_center[0]
    dy = cand_center[1] - ref_center[1]
    
    # Determine basic directional vectors
    if rel == "above" and dy < -10 and abs(dx) < abs(dy):
        return 1.0
    if rel == "below" and dy > 10 and abs(dx) < abs(dy):
        return 1.0
    if rel == "left_of" and dx < -10 and abs(dy) < abs(dx):
        return 1.0
    if rel == "right_of" and dx > 10 and abs(dy) < abs(dx):
        return 1.0
    
    return 0.0
