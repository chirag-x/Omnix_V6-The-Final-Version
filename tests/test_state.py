"""Tests for state and domain matching Phase 1"""
import pytest
import time
from core.state.domain import TaskState, WorldState, WindowState
from core.state.contexts import ConversationContext, ConversationTurn
from core.results import TaskStatus

def test_task_state():
    task = TaskState(task_id="t1", status=TaskStatus.PLANNING, goal="desc")
    task2 = task.with_status(TaskStatus.RUNNING)
    assert task.status == TaskStatus.PLANNING
    assert task2.status == TaskStatus.RUNNING

def test_world_state():
    ws = WorldState(
        timestamp=time.time(),
        window=WindowState(title="cmd", process="cmd.exe", hwnd=123)
    )
    assert ws.window.title == "cmd"

def test_conversation_context():
    ctx = ConversationContext(session_id="s1")
    ctx2 = ctx.append_turn(ConversationTurn(role="user", content="hello", timestamp=time.time()))
    assert len(ctx.turns) == 0
    assert len(ctx2.turns) == 1
