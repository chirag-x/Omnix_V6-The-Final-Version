"""Probe _resolve_exe_for_app across the catalog."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from main import build_engine

def main():
    cfg, eng = build_engine()
    pipeline = eng.pipeline
    disp = pipeline.app_dispatcher
    cap = None
    try:
        router = disp._router
    except AttributeError:
        router = getattr(disp, "_executor", None) or getattr(disp, "router", None)
    print("router:", router)

    # Reach the ApplicationOpenCapability by traversing what FastPath uses
    fast = getattr(disp, "_fast_path", None) or getattr(disp, "fast_path", None) or disp
    open_cap = None
    for attr in ("_app_open", "app_open", "_application_open", "open_capability"):
        if hasattr(fast, attr):
            open_cap = getattr(fast, attr)
            break

    # Try the router's registry
    if open_cap is None and router is not None:
        registry = getattr(router, "_registry", None) or getattr(router, "registry", None)
        if registry is not None:
            for attr in ("_capabilities", "capabilities", "items", "registry"):
                obj = getattr(registry, attr, None)
                if obj is None:
                    continue
                try:
                    iter_obj = obj() if callable(obj) else obj
                except TypeError:
                    iter_obj = obj
                try:
                    for entry in iter_obj:
                        # entry may be (name, cap), cap, or a CapabilityWrapper
                        cand = entry
                        if isinstance(entry, tuple) and len(entry) >= 2:
                            cand = entry[1]
                        cls = cand.__class__.__name__
                        if "ApplicationOpen" in cls:
                            open_cap = cand
                            break
                except TypeError:
                    pass
                if open_cap is not None:
                    break

    if open_cap is None:
        # Brute force search
        import gc
        for obj in gc.get_objects():
            if obj.__class__.__name__ == "ApplicationOpenCapability":
                open_cap = obj
                break

    print("open_cap:", open_cap)
    if open_cap is not None:
        for name in ("Notepad", "notepad", "Chrome", "chrome", "Calculator", "Paint"):
            print(f"  {name!r} -> {open_cap._resolve_exe_for_app(name)!r}")

main()
