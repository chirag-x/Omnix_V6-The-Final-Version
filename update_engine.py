with open('core/omnix_engine.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(
    '''            from vision.perception_adapter import (
                CapabilityPerceptionProvider,
            )
            from vision.router.screenshot_provider import (
                CapabilityScreenshotProvider,
            )
            
            # Setup screenshot provider for the perception adapter
            screenshot_provider = None
            try:
                screenshot_provider = CapabilityScreenshotProvider()
            except Exception:
                # Fallback if capability screenshot fails
                from vision.router.screenshot_provider import (
                    NullScreenshotProvider,
                )
                screenshot_provider = NullScreenshotProvider()

            return CapabilityPerceptionProvider(
                router=self.router,
                screenshot_provider=screenshot_provider,
            )''',
    '''            from vision.perception_adapter import create_default_perception_adapter
            
            return create_default_perception_adapter()'''
)

with open('core/omnix_engine.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated omnix_engine.py")
