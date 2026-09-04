with open('core/omnix_engine.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(
    '''            from vision.perception_adapter import create_default_perception_adapter
            
            return create_default_perception_adapter()''',
    '''            from vision.perception_adapter import create_default_perception_adapter
            from vision.perception_cache import CachedPerceptionProvider, LRUPerceptionCache
            
            base_provider = create_default_perception_adapter()
            cache = LRUPerceptionCache(max_entries=10, default_ttl_s=10.0)
            return CachedPerceptionProvider(underlying_provider=base_provider, cache=cache)'''
)

# And in core/omnix_engine.py where it sets up the ExecutionCycle:
content = content.replace(
    '''                verification_provider=verification_provider,
                perception_cache=None,
                precondition_provider=None,''',
    '''                verification_provider=verification_provider,
                perception_cache=perception_provider,  # Stage 23: Cache invalidation
                precondition_provider=None,'''
)

with open('core/omnix_engine.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated omnix_engine.py for Cache integration")
