import re

with open('vision/observations/targets.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Add explicit fields to TargetCandidate
content = content.replace(
    '''    properties: Dict[str, Any] = field(default_factory=dict) # UIA attributes, yolo class, etc.
    timestamp: float = field(default_factory=time.time)   # timestamp when the candidate was observed''',
    '''    properties: Dict[str, Any] = field(default_factory=dict) # UIA attributes, yolo class, etc.
    timestamp: float = field(default_factory=time.time)   # timestamp when the candidate was observed
    
    # Generic structured perception fields
    element_id: Optional[str] = None
    element_type: Optional[str] = None
    role: Optional[str] = None
    name: Optional[str] = None
    window_id: Optional[int] = None
    application_id: Optional[str] = None
    enabled: bool = True
    visible: bool = True
    focused: bool = False
    selected: bool = False
    value: Optional[str] = None'''
)

with open('vision/observations/targets.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated targets.py")
