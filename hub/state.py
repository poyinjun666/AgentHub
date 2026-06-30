"""
任务状态机 - 强制合法转换，非法跳转直接拒绝并审计
状态流转：
    created → submitted → checking ─┬→ done       (通过)
                                   ├→ reworking  (打回)
                                   └→ blocked    (升级人工)
    reworking → submitted (循环，受 max_retries 限制)
    blocked   → submitted | cancelled
"""


class TaskState:
    CREATED     = "created"
    SUBMITTED   = "submitted"
    CHECKING    = "checking"
    DONE        = "done"
    REWORKING   = "reworking"
    BLOCKED     = "blocked"
    CANCELLED   = "cancelled"
    FAILED      = "failed"


# 合法转换白名单
_VALID_TRANSITIONS = {
    TaskState.CREATED:     {TaskState.SUBMITTED},
    TaskState.SUBMITTED:   {TaskState.CHECKING, TaskState.CANCELLED},
    TaskState.CHECKING:    {TaskState.DONE, TaskState.REWORKING, TaskState.BLOCKED, TaskState.FAILED},
    TaskState.REWORKING:   {TaskState.SUBMITTED, TaskState.CANCELLED},
    TaskState.BLOCKED:     {TaskState.SUBMITTED, TaskState.CANCELLED},
    TaskState.DONE:        set(),       # 终态
    TaskState.FAILED:      set(),       # 终态
    TaskState.CANCELLED:   set(),       # 终态
}

TERMINAL_STATES = {TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED}


class IllegalTransition(Exception):
    """非法状态跳转"""
    def __init__(self, from_state: str, to_state: str, task_id: str = ""):
        self.from_state = from_state
        self.to_state = to_state
        self.task_id = task_id
        super().__init__(f"Illegal transition: {from_state} → {to_state} (task={task_id})")


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in _VALID_TRANSITIONS.get(from_state, set())


def assert_transition(from_state: str, to_state: str, task_id: str = ""):
    if not can_transition(from_state, to_state):
        raise IllegalTransition(from_state, to_state, task_id)


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES