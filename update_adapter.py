import re

with open('vision/perception_adapter.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace the observe method completely
observe_impl = '''    async def observe(
        self,
        request: PerceptionRequest,
        cancellation_token: Optional[Any] = None,
    ) -> PerceptionResult:
        """
        Observe current computer state and return structured observations.
        Stage 23: Complete multi-layered sweep (Window State + UIA + OCR)
        """
        start_time = time.time()
        
        # Check for cancellation
        if cancellation_token and hasattr(cancellation_token, 'cancelled') and cancellation_token.cancelled:
            return PerceptionResult(
                observation_id="",
                timestamp=None,
                screen=self._get_screen_info(),
                status=PerceptionStatus.CANCELLED,
                duration_ms=(time.time() - start_time) * 1000
            )

        try:
            needs_screenshot = self._needs_screenshot(request)
            screenshot_path = None
            screenshot_bytes = None
            
            # 1. LAYER B: Screenshot
            if needs_screenshot and request.include_screenshot:
                import tempfile
                import os
                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                    screenshot_path = tmp.name

                try:
                    result_path = self._screenshot_provider.capture(path=screenshot_path)
                    if result_path and os.path.exists(result_path):
                        with open(result_path, 'rb') as f:
                            screenshot_bytes = f.read()
                finally:
                    if screenshot_path and os.path.exists(screenshot_path):
                        os.unlink(screenshot_path)

            # 2. LAYER A: System / Window State
            active_window = None
            windows_list = []
            applications_set = set()
            
            if request.include_window_context:
                active_window = self._get_window_context()
                
                # Enumerate all visible windows
                try:
                    import ctypes
                    from ctypes import wintypes
                    user32 = ctypes.windll.user32
                    
                    def enum_windows_proc(hwnd, lParam):
                        if user32.IsWindowVisible(hwnd):
                            length = user32.GetWindowTextLengthW(hwnd)
                            if length > 0:
                                buffer = ctypes.create_unicode_buffer(length + 1)
                                user32.GetWindowTextW(hwnd, buffer, length + 1)
                                title = buffer.value
                                
                                rect = wintypes.RECT()
                                bounds = None
                                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                                    bounds = (rect.left, rect.top, rect.right, rect.bottom)
                                
                                # Extract process
                                pid = ctypes.c_ulong()
                                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                                app_name = None
                                try:
                                    import psutil
                                    app_name = psutil.Process(pid.value).name().lower().rstrip('.exe')
                                    if app_name:
                                        applications_set.add(app_name)
                                except Exception:
                                    pass
                                    
                                windows_list.append(WindowContext(
                                    hwnd=hwnd,
                                    title=title,
                                    application=app_name,
                                    bounds=bounds,
                                    is_foreground=(active_window and active_window.hwnd == hwnd)
                                ))
                        return True
                        
                    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
                    user32.EnumWindows(WNDENUMPROC(enum_windows_proc), 0)
                except Exception:
                    pass
            
            # 3. LAYER C: Elements & Text Regions
            target_query = "*"
            elements = []
            text_regions = []
            
            try:
                # Run UIA Strategy specifically for elements
                try:
                    from vision.strategies.uia_strategy import UIAStrategy
                    uia = UIAStrategy()
                    if uia:
                        elements = uia.find_targets(target_query=target_query, image_path=None)
                except ImportError:
                    pass
                except Exception:
                    pass
                    
                # Run OCR Strategy specifically for text if requested
                if request.include_ocr and screenshot_bytes:
                    try:
                        from vision.strategies.ocr_strategy import OCRStrategy
                        ocr = OCRStrategy()
                        if ocr:
                            import tempfile
                            import os
                            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                                tmp_path = tmp.name
                                tmp.write(screenshot_bytes)
                            try:
                                text_regions = ocr.find_targets(target_query=target_query, image_path=tmp_path)
                            finally:
                                if os.path.exists(tmp_path):
                                    os.unlink(tmp_path)
                    except ImportError:
                        pass
                    except Exception:
                        pass
                        
            except (AmbiguityError, TargetNotGroundedError):
                pass
                
            # Filter and deduplicate elements and text_regions if necessary
            # (Stage 23 fusion)
            
            # Populate candidates for legacy support
            all_candidates = elements + text_regions
            
            perception_sources = self._convert_observation_sources(
                [getattr(c, 'source_type', ObservationSource.DERIVED) for c in all_candidates]
            )
            
            status = self._determine_perception_status(
                request=request,
                candidates=all_candidates,
                screenshot_available=screenshot_bytes is not None and request.include_screenshot,
                needs_screenshot=needs_screenshot
            )

            duration_ms = (time.time() - start_time) * 1000

            return PerceptionResult(
                observation_id="",
                timestamp=None,
                screen=self._get_screen_info(),
                screenshot=screenshot_bytes,
                candidates=tuple(all_candidates),
                window_context=active_window,
                active_window=active_window,
                windows=tuple(windows_list),
                applications=tuple(applications_set),
                elements=tuple(elements),
                text_regions=tuple(text_regions),
                sources=tuple(perception_sources),
                duration_ms=duration_ms,
                status=status,
                metadata={
                    "needs_screenshot": needs_screenshot,
                    "screenshot_used": screenshot_bytes is not None
                }
            )

        except Exception as e:
            import traceback
            traceback.print_exc()
            duration_ms = (time.time() - start_time) * 1000
            return PerceptionResult(
                observation_id="",
                timestamp=None,
                screen=self._get_screen_info(),
                status=PerceptionStatus.FAILED,
                duration_ms=duration_ms,
                metadata={
                    "error": str(e),
                    "error_type": type(e).__name__
                }
            )'''

pattern = r'    async def observe\(.*?return PerceptionResult\(.*?error_type": type\(e\)\.__name__\n                \}\n            \)'
new_content = re.sub(pattern, observe_impl, content, flags=re.DOTALL)

with open('vision/perception_adapter.py', 'w', encoding='utf-8') as f:
    f.write(new_content)
print("Updated perception_adapter.py")
