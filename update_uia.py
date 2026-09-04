with open('vision/strategies/uia_strategy.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(
    '''                    name = (el.window_text() or "").lower()
                    if not name:
                        continue
                    # Substring containment in either direction --
                    # UIA is deterministic and live.
                    if lower_query in name or name in lower_query:''',
    '''                    name = (el.window_text() or "").lower()
                    
                    # Substring containment in either direction, or wildcard
                    # UIA is deterministic and live.
                    is_match = False
                    if lower_query == "*":
                        is_match = True
                    elif name and (lower_query in name or name in lower_query):
                        is_match = True
                        
                    if is_match:'''
)

# And add the explicit structured properties
content = content.replace(
    '''                                    properties={
                                        "control_type": el.friendly_class_name()
                                    },
                                )''',
    '''                                    properties={
                                        "control_type": el.friendly_class_name()
                                    },
                                    element_type=el.friendly_class_name(),
                                    name=el.window_text(),
                                    window_id=win.handle if hasattr(win, 'handle') else None,
                                    enabled=el.is_enabled(),
                                    visible=el.is_visible(),
                                )'''
)

with open('vision/strategies/uia_strategy.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated uia_strategy.py")
