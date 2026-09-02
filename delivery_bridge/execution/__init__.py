"""ExecutionBridge：按领域拆成若干 Mixin 后再组合回来。

类内部全是 self.xxx() 互调，横跨领域的调用很多（比如每个阶段跑完都要走
turns 里的跟进和 sync 里的归档），拆成互相持有的独立对象等于重写一遍。
Mixin 组合能让调用点一行不改，同时把每个领域收进自己的文件。
"""

from __future__ import annotations

from .core import CoreMixin
from .sync import SyncMixin
from .naming import NamingMixin
from .planning import PlanningMixin
from .environment import EnvironmentMixin
from .requirement_testing import RequirementTestingMixin
from .requirement_review import RequirementReviewMixin
from .fine_tuning import FineTuningMixin
from .task_testing import TaskTestingMixin
from .git import GitMixin
from .queue import QueueMixin
from .conversation import ConversationMixin
from .documents import DocumentsMixin
from .prototype import PrototypeMixin
from .turns import TurnsMixin
from .usage import UsageMixin


class ExecutionBridge(
    CoreMixin,
    SyncMixin,
    NamingMixin,
    PlanningMixin,
    EnvironmentMixin,
    RequirementTestingMixin,
    RequirementReviewMixin,
    FineTuningMixin,
    TaskTestingMixin,
    GitMixin,
    QueueMixin,
    ConversationMixin,
    DocumentsMixin,
    PrototypeMixin,
    TurnsMixin,
    UsageMixin,
):
    pass


__all__ = ["ExecutionBridge"]
