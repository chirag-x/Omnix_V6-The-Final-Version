with open('vision/strategies/ocr_strategy.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(
    '''            text_str = (text or "").lower()
            if not text_str:
                continue
            if lower_query in text_str or text_str in lower_query:''',
    '''            text_str = (text or "").lower()
            if not text_str and lower_query != "*":
                continue
            
            is_match = False
            if lower_query == "*":
                is_match = True
            elif text_str and (lower_query in text_str or text_str in lower_query):
                is_match = True
                
            if is_match:'''
)

# And add the explicit structured properties
content = content.replace(
    '''                        bbox=(int(tl_x), int(tl_y), int(br_x), int(br_y)),
                        confidence=prob * self.source_reliability,
                        text=text,
                    )''',
    '''                        bbox=(int(tl_x), int(tl_y), int(br_x), int(br_y)),
                        confidence=prob * self.source_reliability,
                        text=text,
                        element_type="text_region",
                        name=text,
                    )'''
)

with open('vision/strategies/ocr_strategy.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated ocr_strategy.py")
