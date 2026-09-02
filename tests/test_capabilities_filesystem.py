import pytest
import os
import tempfile
from core.capabilities.filesystem import FileReadCapability, FileWriteCapability
from core.results import CapabilityStatus

@pytest.mark.asyncio
async def test_file_write_read_roundtrip():
    with tempfile.TemporaryDirectory() as temp_dir:
        test_file = os.path.join(temp_dir, "test.txt")
        test_content = "Hello, Omnix V6!"
        
        # Test Write
        write_cap = FileWriteCapability()
        write_params = {"path": test_file, "content": test_content}
        write_result = await write_cap.execute(write_params)
        
        assert write_result.status == CapabilityStatus.VERIFIED
        assert write_result.error is None
        assert write_result.details["path"] == test_file
        
        # Verify file exists
        assert os.path.exists(test_file)
        
        # Test Read
        read_cap = FileReadCapability()
        read_params = {"path": test_file}
        read_result = await read_cap.execute(read_params)
        
        assert read_result.status == CapabilityStatus.VERIFIED
        assert read_result.error is None
        assert read_result.details["content"] == test_content

@pytest.mark.asyncio
async def test_file_read_not_found():
    with tempfile.TemporaryDirectory() as temp_dir:
        test_file = os.path.join(temp_dir, "nonexistent.txt")
        
        read_cap = FileReadCapability()
        read_params = {"path": test_file}
        read_result = await read_cap.execute(read_params)
        
        assert read_result.status == CapabilityStatus.FAILED
        assert "not found" in str(read_result.error).lower()

@pytest.mark.asyncio
async def test_file_path_must_be_absolute():
    read_cap = FileReadCapability()
    read_params = {"path": "relative/path.txt"}
    read_result = await read_cap.execute(read_params)
    
    assert read_result.status == CapabilityStatus.FAILED
    assert "absolute" in str(read_result.error).lower()
