with open('vision/perception_contract.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(
    '''    # Observation candidates from perception sources
    candidates: Tuple[TargetCandidate, ...] = field(default_factory=tuple)

    # Window context for the observation
    window_context: Optional[WindowContext] = None''',
    '''    # Observation candidates from perception sources (legacy support)
    candidates: Tuple[TargetCandidate, ...] = field(default_factory=tuple)

    # Window context for the observation (legacy support)
    window_context: Optional[WindowContext] = None
    
    # Stage 23: Rich Structured Perception
    active_window: Optional[WindowContext] = None
    windows: Tuple[WindowContext, ...] = field(default_factory=tuple)
    applications: Tuple[str, ...] = field(default_factory=tuple)
    elements: Tuple[TargetCandidate, ...] = field(default_factory=tuple)
    text_regions: Tuple[TargetCandidate, ...] = field(default_factory=tuple)'''
)

with open('vision/perception_contract.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated perception_contract.py")
